"""**전방 깊이 격차의 기전** — 닫을 수 있는 격차인가, 원리상 안 닫히는 격차인가.

## 이 모듈의 지위 — **탐색이다. 판정이 아니다**

세 번째 삽이다(`specs/w3_g2_identification.md`). `docs/64`(첫 삽)와 `docs/65`(둘째 삽)가
같은 축에서 멈췄다: **앵커 뒤 300 초의 실현 막대 수(전방 깊이)가 실제와 위약에서
따로 놀고**, `nbar60`(60 초 사전 밀도)로도 `nbar300`(300 초 사전 밀도)으로도 그 격차가
안 닫혔다(|격차| 중앙값 19.5 → 39.5 막대, `docs/65` §4).

명세 §1 의 가설: **실현 전방 깊이는 처치후(post-treatment) 변수다** — 사건이 일어난 것
자체가 그 값을 바꾼다. 참이면 사전(앵커 이전) 정합축을 아무리 잘 골라도 실현 격차는
원리상 안 닫힌다 — 격차가 교란이 아니라 **처치 반응 그 자체**이기 때문이다.

**가설이지 결론이 아니다. 이 모듈이 가른다.** 반증되면 반증됐다고 적는다.

## 무엇을 재나 — 셋. 전부 `ranking_forward_path.run()` 의 표본 그 자체 위에서

`ranking_forward_path.run()` 을 **수정 없이 그대로 호출**한다. 추첨 씨앗까지 같으므로
여기 나오는 짝은 `docs/65` 가 실은 짝과 **같은 짝**이다. 새 표본을 만들면 두 문서의
수치가 서로 대조되지 않는다.

### [M2] 짝 수준 깊이 반응 — **이 모듈의 본체**

짝 하나 = (실제 앵커 1, 같은 종목·같은 세션·밴드 안 위약 추첨 <=3). 짝마다

    d_fwd = 실제의 전방 막대 수 - 위약 전방 막대 수의 짝 내 평균   (양수 = 실제가 깊다)

를 내고, 칸(랭킹 x 사건 x 칸)별로 평균·중앙값·양(+) 비율과 **거래일 군집 부트스트랩
CI** 를 낸다(군집 5 미만이면 CI 없음 — `ranking_forward_path` 와 같은 규율·같은 씨앗).

- **일차 팔은 `placebo_vol_density_nbar300_matched` 다.** 이 팔에서는 짝 안에서
  `rv60`·`nbar60`·`nbar300` 이 전부 +-20% 밴드 안이라, d_fwd 가 0 이 아니면 그것을
  사전 상태 불균형으로 돌릴 수 없다 — 남는 설명은 **사건 그 자체**뿐이다.
- `placebo_vol_density_matched` 는 **맥락 팔**이다: 짝 내 `nbar300` 잔차(d_nbar300)가
  통제되지 않으므로 그 팔의 d_fwd 는 d_nbar300 열과 **함께** 읽어야 한다.
- 짝 내 사전 잔차(d_nbar300, d_nbar60)와 마감까지 남은 시간의 차(d_t_to_close)를
  같은 표에 싣는다 — d_fwd 가 창 절단(`docs/64` §9 의 33 분 어긋남)에서 나오는지
  보는 눈이다. 절단이 불가능한 부분집합(실제·위약 모두 마감까지 300 초 이상)의
  d_fwd 도 점추정으로 병기한다(**CI 없음** — 가족을 안 늘린다, 아래 다중검정).

### [M3] `docs/65` [11] 의 격차 통계는 이 표본 크기에서 정보인가 — 순열 진단

`docs/65` [11] 이 쓴 통계 그대로: fwd_gap = (위약 전방 막대 p50) - (실제 전방 막대 p50),
칸별 풀링. 짝 안에서 실제/위약 라벨은 귀무(사건이 깊이에 아무 일도 안 함) 아래
교환 가능하므로, 짝마다 (1+k)개 값 중 하나를 무작위로 "실제"로 뽑는 순열 10,000 회로
|fwd_gap| 의 귀무 분포를 만들고 관측값의 양측 p 를 낸다.

**이것이 "19.5 -> 39.5 로 커졌다" 를 해석하는 자다.** 관측 격차가 귀무 분포의 몸통에
들면, "격차가 안 닫혔다"는 관측은 이 짝 수(14~82)에서 잡음과 구분되지 않는 것이고,
격차 닫힘을 정합 성공의 검사식으로 쓰는 것 자체가 이 표본에서 무리다.

### [M1] 무처치 순간에서 사전 깊이가 전방 깊이를 얼마나 박는가 — 진단, CI 없음

`placebo_unmatched` 추첨(같은 종목들의 밴드 없는 다른 순간)에서 `nbar300` 과 실현
전방 막대 수의 스피어만 상관(풀링 + 종목 내), 그리고 비 fwd/nbar300 의 사분위.
낮으면: **어떤** 사전 축을 밴드로 걸어도 실현 깊이는 짝 단위로 안 박힌다 — 닫을 수
있는 것은 기대값의 격차뿐이고, 실현 격차 0 을 목표로 삼은 것 자체가 성립하지 않는다.
`nbar60` 의 같은 상관을 옆에 놓아 `docs/65` §2-2 의 "창 길이 불일치" 진단이 무처치
데이터에서 실제로 보이는지도 적는다.

## ★ 판정식 — **실행 전에 적는다** (`docs/65` §2-3 의 규율)

아래 규칙은 어느 칸이 어느 쪽으로 움직였는지와 무관하게, 측정량의 시간 방향과
표본 구조에서만 나온다. 산출물 JSON 에 `design.decision_rules` 로 그대로 박힌다.

1. **[M2] 일차 팔에서 CI 가 나오는 칸들**(군집 5 이상은 `TOSS_..._VOLUME` 세 칸뿐이라는
   것이 실행 전에 알려져 있다 — `docs/65` §7, `TOP_GAINERS` 는 탐색 팔 거래일이 4 개)
   **의 과반이 같은 부호로 0 을 제외하면**: 사건이 사전 상태를 맞춘 뒤에도 앵커 뒤
   테이프를 바꾼다. 실현 전방 깊이는 처치후 변수이고, 사전 정합축 추가로 실현 격차를
   닫으려는 시도는 원리상 과녁이 없다. 명세 §1-1 은 **(나)** 로 간다.
2. **[M2] 그 CI 들이 전부 0 을 교차하고 [M3] 의 p 가 (러너가 센 순열 가족 수로
   본페로니 보정한 문턱에서) 특기할 것이 없으면**: 이 짝 수에서 격차 관측은 잡음과
   구분되지 않는다. "안 닫혔다"는 두 삽의 관측은 기전의 증거가 아니고, **실현 격차
   닫힘이라는 검사식 자체가 이 표본 크기에서 판별력이 없다.** 이 경우 §1 의 가설은
   이 표본으로 지지되지 않지만, 정합 경로의 다음 삽 역시 같은 검사식으로는 성공을
   보일 수 없다는 뜻이 된다.
3. **[M1] 무처치 스피어만이 1 에서 멀면**: 사전 정보가 실현 전방 깊이를 짝 단위로
   못 박는다. 실현 격차 0 은 달성 가능한 정합 목표가 아니었고, 닫을 수 있는 것은
   조건부 기대값의 격차뿐이다.
4. 1 과 2 가 **둘 다 아닌** 조합(부호가 갈리거나 일부만 제외)이면 "이 표본으로 못
   가른다"라고 적는다 — 명세 §6 이 그것을 정당한 결론으로 명시한다.

## 다중검정 — 러너가 자기 가족을 센다 (`STRATEGY-VERDICTS` §4.4-F)

- **CI 가족**: [M2] 의 (칸 x 팔) 레코드 수. 러너가 세고 본페로니 전/후를 둘 다 낸다.
  절단 없는 부분집합은 점추정만 내므로 가족에 안 들어간다 — 그래서 CI 를 안 붙인다.
- **순열 가족**: [M3] 의 레코드 수. p 옆에 0.05/가족수 문턱을 같이 인쇄한다.
- [M1] 은 CI 도 p 도 없는 진단이다.

## 하지 않는 것

- **판정하지 않는다.** 통과/실패/성립을 쓰지 않는다. `E` 를 고르지 않는다.
- **`max_ret` 차이를 다시 내지 않는다.** 그건 `docs/65` [4] 다. 여기는 기전만 본다.
- **확증 팔을 열 수 없다.** `--all-sessions` 에 해당하는 스위치가 이 모듈에는 **없다.**
  `ranking_forward_path.run()` 을 기본값(탐색 팔 9 세션)으로만 부르고, 산출물의 세션
  목록이 `EXPLORATION_SESSIONS` 의 부분집합이 아니면 예외로 죽는다.
- **층(tier) 칸을 열지 않는다.** 기전 질문에 층은 필요 없고 가족만 커진다.
  전층(all) x 주 칸 셋 x 랭킹 둘 x 팔 둘만 본다. — 실행 전에 적었다.
- 라이브 API 콜 0. DB 는 `mode=ro`(`hires_events.open_ro`). 다른 워크트리에 쓰기 0.

실행: `python -m tossmon.analysis.measure.forward_depth_mechanism [db]
[--out DIR] [--name NAME]` -> `out/<name>.json`. **콘솔 ASCII.**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from tossmon.analysis import hires_events as HE
from tossmon.analysis.measure import ranking_forward_path as RFP

#: 세션 길이(초). 정규장 13:30~20:00 UTC — 마감까지 남은 시간(t_to_close)의 자다.
SESSION_LEN_S = HE.REGULAR_CLOSE_S - HE.REGULAR_OPEN_S

#: 실현 전방 깊이 지표와 사전 대리. `ranking_forward_path` 의 정의를 그대로 쓴다.
FWD_BARS = f"n_bars_{RFP.HEADLINE_H}s"

#: 기전 질문이 보는 팔 둘. 일차 = `nbar300` 정합 팔(짝 내 사전 잔차가 밴드로 묶인다),
#: 맥락 = 그 직전 팔(잔차가 자유라 d_nbar300 열과 함께 읽는다). 실행 전에 적었다.
MECH_ARMS = RFP.TIER_ARMS
PRIMARY_ARM = "placebo_vol_density_nbar300_matched"

#: 순열 진단. 반복 수는 부트스트랩과 같은 값을 쓰고, 씨앗은 이 태스크의 날짜다
#: (레포 관례 — `ranking_forward_path.SEED` 의 사유와 같다).
N_PERM = RFP.BOOTSTRAP_N
PERM_SEED = 20260822

#: [M1] 종목 내 스피어만을 낼 최소 추첨 수. 이보다 적은 종목은 상관이 뜻이 없다.
MIN_DRAWS_PER_SYMBOL = 10

#: 진단(CI 없음)에 붙는 사유 — `depth_strata` 의 규율과 같다.
NO_CI_DIAGNOSTIC = ("diagnostic, not an estimate - no CI is attached on purpose "
                    "(adding one would enlarge the corrected family without "
                    "adding information)")


# --------------------------------------------------------------------------- #
# 짝 복원 — 러너가 이미 만든 추첨에서, 새 추첨 없이
# --------------------------------------------------------------------------- #
def pair_table(res: dict) -> dict:
    """`ranking_forward_path.run()` 결과에서 **짝 수준** 배열을 복원한다.

    러너는 세션마다 `pairing`(추첨 원장: slot=사건 번호), `real_paired`(짝 지은
    실제의 원값), `raw`(위약 원값)를 같은 반복 안에서 만든다. 세션 처리 순서가
    날짜순이고 `raw` 의 키도 날짜라, `sorted(raw)` 와 `pairing` 리스트가 1:1 이다.
    **그 정렬 가정을 믿지 않고 검산한다** — 행 수가 원장과 어긋나면 예외로 죽는다.
    조용히 계속하면 짝이 뒤섞인 채 표가 나온다(§4.4-D 의 "짝 161 vs 정합 160" 사고).

    반환: {(랭킹, 사건, 칸, 팔): {"by_session": {세션: {짝 배열들}}}}.
    짝 배열의 부호 규약은 전부 **실제 - 위약(짝 내 평균)** 이다.
    """
    out = {}
    for key, box in sorted(res["cells"].items()):
        rtype, kind, cell, tier = key.split("|")
        if tier != "all" or (kind, cell) not in RFP.PRIMARY_CELLS:
            continue
        for arm in MECH_ARMS:
            a = box["arms"].get(arm)
            if not a or not a["pairing"]:
                continue
            sess_names = sorted(a["raw"])
            if len(sess_names) != len(a["pairing"]):
                raise ValueError(
                    f"{key} {arm}: {len(a['pairing'])} pairing ledgers vs "
                    f"{len(sess_names)} raw sessions - alignment lost")
            by_sess = {}
            for sess, d in zip(sess_names, a["pairing"]):
                rr = a["real_paired"][sess]
                pr = a["raw"][sess]
                sel = np.flatnonzero(np.asarray(d["paired"], dtype=bool))
                if int(rr["_symbol"].size) != int(sel.size):
                    raise ValueError(
                        f"{key} {arm} {sess}: real_paired rows "
                        f"{int(rr['_symbol'].size)} != paired fires {int(sel.size)}")
                if int(pr["_symbol"].size) != int(np.asarray(d["sym"]).size):
                    raise ValueError(
                        f"{key} {arm} {sess}: placebo rows "
                        f"{int(pr['_symbol'].size)} != draws "
                        f"{int(np.asarray(d['sym']).size)}")
                if sel.size == 0:
                    continue
                slot = np.asarray(d["slot"], dtype="int64")
                row_of = np.full(int(sel.max()) + 1, -1, dtype="int64")
                row_of[sel] = np.arange(sel.size, dtype="int64")
                rows = row_of[slot]
                if (rows < 0).any():
                    raise ValueError(f"{key} {arm} {sess}: a draw points at an "
                                     f"unpaired fire - ledger corrupt")
                m = int(sel.size)
                cnt = np.bincount(rows, minlength=m).astype("float64")
                if (cnt == 0).any():
                    raise ValueError(f"{key} {arm} {sess}: a paired fire has no "
                                     f"draws - ledger corrupt")

                def _mean_by_pair(vals):
                    v = np.asarray(vals, dtype="float64")
                    return np.bincount(rows, weights=v, minlength=m) / cnt

                r_fwd = np.asarray(rr[FWD_BARS], "float64")
                p_fwd = np.asarray(pr[FWD_BARS], "float64")
                r_tin = np.asarray(rr["t_in_session_s"], "float64")
                p_tin = np.asarray(pr["t_in_session_s"], "float64")
                p_tin_max = np.full(m, -np.inf)
                np.maximum.at(p_tin_max, rows, p_tin)
                # 짝별 위약 원값 (순열용). rows 가 짝 번호이므로 그대로 가른다.
                order = np.argsort(rows, kind="stable")
                bounds = np.searchsorted(rows[order], np.arange(m + 1))
                plac_vals = [p_fwd[order[bounds[i]:bounds[i + 1]]]
                             for i in range(m)]
                by_sess[sess] = {
                    "d_fwd": r_fwd - _mean_by_pair(p_fwd),
                    "d_nbar300": (np.asarray(rr[RFP.FWD_PROXY_KEY], "float64")
                                  - _mean_by_pair(pr[RFP.FWD_PROXY_KEY])),
                    "d_nbar60": (np.asarray(rr["nbar60"], "float64")
                                 - _mean_by_pair(pr["nbar60"])),
                    # t_to_close = 세션길이 - t_in_session. 차이는 아래처럼 뒤집힌다.
                    "d_t_to_close": _mean_by_pair(p_tin) - r_tin,
                    "untruncated": ((SESSION_LEN_S - r_tin >= RFP.HEADLINE_H)
                                    & (SESSION_LEN_S - p_tin_max
                                       >= RFP.HEADLINE_H)),
                    "real_fwd": r_fwd,
                    "plac_fwd": plac_vals,
                    "symbol": np.asarray(rr["_symbol"], dtype=object),
                }
            if by_sess:
                out[(rtype, kind, cell, arm)] = {"by_session": by_sess}
    return out


# --------------------------------------------------------------------------- #
# [M2] 짝 수준 깊이 반응
# --------------------------------------------------------------------------- #
def cluster_bootstrap_mean(by_session: dict, *, n_boot: int = RFP.BOOTSTRAP_N,
                           seed: int = RFP.BOOTSTRAP_SEED,
                           min_clusters: int = RFP.MIN_SESSION_CLUSTERS,
                           n_comparisons: int = 1) -> dict:
    """짝 차이 평균의 **거래일 군집** 부트스트랩 CI.

    `ranking_forward_path.cluster_bootstrap_diff` 와 같은 규율(군집 5 미만이면 CI 를
    코드가 막는다, §4.4-B)·같은 반복 수·같은 씨앗을 쓴다 — 표본이 하나(차이)라는
    것만 다르다. 새 자유변수를 만들지 않는다.
    """
    keys = [k for k in sorted(by_session)
            if np.isfinite(np.asarray(by_session[k], "float64")).any()]
    arrs = [np.asarray(by_session[k], "float64") for k in keys]
    arrs = [a[np.isfinite(a)] for a in arrs]
    v = np.concatenate(arrs) if arrs else np.zeros(0)
    out = {"n_clusters": len(keys), "clusters": keys, "n": int(v.size),
           "mean": float(v.mean()) if v.size else None,
           "ci95": None, "crosses_zero": None,
           "n_comparisons": int(n_comparisons),
           "ci_bonferroni": None, "crosses_zero_bonferroni": None,
           "ci_withheld": None}
    if len(keys) < min_clusters:
        out["ci_withheld"] = (
            f"trading-day clusters {len(keys)} < {min_clusters}: no pooled CI is "
            f"produced (STRATEGY-VERDICTS 4.4-B). The runner enforces this, not "
            f"discipline.")
        return out
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(keys), size=(n_boot, len(keys)))
    s = np.array([a.sum() for a in arrs])
    n = np.array([a.size for a in arrs])
    means = s[pick].sum(axis=1) / np.maximum(n[pick].sum(axis=1), 1)
    lo, hi = np.quantile(means, [0.025, 0.975])
    out["ci95"] = [float(lo), float(hi)]
    out["crosses_zero"] = bool(lo <= 0.0 <= hi)
    a = 0.05 / max(1, int(n_comparisons))
    blo, bhi = np.quantile(means, [a / 2, 1.0 - a / 2])
    out["ci_bonferroni"] = [float(blo), float(bhi)]
    out["crosses_zero_bonferroni"] = bool(blo <= 0.0 <= bhi)
    return out


def paired_depth_response(pairs: dict) -> list:
    """[M2] 칸 x 팔 하나마다 짝 차이 레코드 하나.

    보정 분모는 **여기서 만든 레코드 수**다 — CI 가 유보된 칸도 들여다본 칸이므로
    센다(`STRATEGY-VERDICTS` §4.4-F). 절단 없는 부분집합은 점추정만 낸다(CI 가족을
    늘리지 않기 위해서다 — 모듈 독스트링의 다중검정 절).
    """
    n_comp = len(pairs)
    recs = []
    for (rtype, kind, cell, arm), p in sorted(pairs.items()):
        bs = p["by_session"]
        cat = {k: np.concatenate([bs[s][k] for s in sorted(bs)])
               for k in ("d_fwd", "d_nbar300", "d_nbar60", "d_t_to_close",
                         "untruncated")}
        sym = np.concatenate([bs[s]["symbol"] for s in sorted(bs)])
        d = cat["d_fwd"]
        u = cat["untruncated"].astype(bool)
        recs.append({
            "ranking_type": rtype, "kind": kind, "cell": cell, "arm": arm,
            "primary_arm": arm == PRIMARY_ARM,
            "n_pairs": int(d.size), "n_sessions": len(bs),
            "pairs_by_session": {s: int(bs[s]["d_fwd"].size) for s in sorted(bs)},
            "d_fwd_bars": {
                "mean": float(d.mean()), "p50": float(np.median(d)),
                "share_positive": float((d > 0).mean()),
            },
            "d_fwd_bars_scales": RFP.two_scales(d, sym),
            "d_nbar300_p50": float(np.median(cat["d_nbar300"])),
            "d_nbar60_p50": float(np.median(cat["d_nbar60"])),
            "d_t_to_close_s_p50": float(np.median(cat["d_t_to_close"])),
            "untruncated": {
                "n": int(u.sum()),
                "d_fwd_mean": float(d[u].mean()) if u.any() else None,
                "d_fwd_p50": float(np.median(d[u])) if u.any() else None,
                "note": NO_CI_DIAGNOSTIC,
            },
            "diff": cluster_bootstrap_mean(
                {s: bs[s]["d_fwd"] for s in bs}, n_comparisons=n_comp),
        })
    return recs


# --------------------------------------------------------------------------- #
# [M3] 격차 통계의 순열 진단
# --------------------------------------------------------------------------- #
def gap_permutation(pairs: dict, *, n_perm: int = N_PERM,
                    seed: int = PERM_SEED) -> list:
    """[M3] `docs/65` [11] 의 fwd_gap(위약 p50 - 실제 p50)을 짝 내 교환으로 순열한다.

    귀무(사건이 앵커 뒤 깊이에 아무 일도 안 함) 아래에서 짝 안의 (1+k)개 값은 교환
    가능하다 — 위약이 같은 종목·같은 세션·사전 밴드 안에서 뽑혔기 때문이다. 짝마다
    하나를 "실제"로 다시 뽑는 것을 전 짝에 독립으로 10,000 회 반복해 |fwd_gap| 의
    귀무 분포를 만든다. p 는 (넘은 수 + 1)/(반복 + 1) — 0 이 나오지 않는 꼴이다.

    **관측 fwd_gap 은 `docs/65` [11] 과 같은 정의**(세션 풀링, p50 차)라 그 표와
    숫자가 그대로 이어진다.
    """
    rng = np.random.default_rng(seed)
    recs = []
    for (rtype, kind, cell, arm), p in sorted(pairs.items()):
        bs = p["by_session"]
        real = np.concatenate([bs[s]["real_fwd"] for s in sorted(bs)])
        plac_lists = [v for s in sorted(bs) for v in bs[s]["plac_fwd"]]
        plac_all = np.concatenate(plac_lists)
        obs = float(np.median(plac_all) - np.median(real))
        n_pairs = int(real.size)
        # 짝마다: 값 (1+k)개 중 순열 choice 하나가 "실제", 나머지가 "위약".
        real_mat = np.empty((n_perm, n_pairs))
        plac_cols = []
        for i in range(n_pairs):
            v = np.concatenate([[real[i]], plac_lists[i]])
            mm = int(v.size)
            drop = np.stack([np.delete(v, j) for j in range(mm)])  # (mm, mm-1)
            c = rng.integers(0, mm, size=n_perm)
            real_mat[:, i] = v[c]
            plac_cols.append(drop[c])
        plac_mat = np.concatenate(plac_cols, axis=1)
        gaps = np.median(plac_mat, axis=1) - np.median(real_mat, axis=1)
        recs.append({
            "ranking_type": rtype, "kind": kind, "cell": cell, "arm": arm,
            "n_pairs": n_pairs,
            "observed_gap": obs,
            "null_abs_gap_p50": float(np.median(np.abs(gaps))),
            "null_abs_gap_p90": float(np.quantile(np.abs(gaps), 0.90)),
            "p_two_sided": float((int((np.abs(gaps) >= abs(obs)).sum()) + 1)
                                 / (n_perm + 1)),
            "n_perm": int(n_perm), "seed": int(seed),
        })
    return recs


# --------------------------------------------------------------------------- #
# [M1] 무처치 예측력 진단
# --------------------------------------------------------------------------- #
def spearman(x, y) -> float | None:
    """스피어만 순위 상관 — 평균 순위로 동순위를 처리한다. 표본 3 미만이면 None."""
    x = np.asarray(x, "float64")
    y = np.asarray(y, "float64")
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size < 3:
        return None

    def _ranks(v):
        order = np.argsort(v, kind="stable")
        r = np.empty(v.size, "float64")
        r[order] = np.arange(1, v.size + 1, dtype="float64")
        # 같은 값 묶음에 평균 순위를 준다
        uniq, inv, cnt = np.unique(v, return_inverse=True, return_counts=True)
        sums = np.bincount(inv, weights=r)
        return (sums / cnt)[inv]

    rx, ry = _ranks(x), _ranks(y)
    sx, sy = rx.std(), ry.std()
    if sx == 0 or sy == 0:
        return None
    return float(((rx - rx.mean()) * (ry - ry.mean())).mean() / (sx * sy))


def depth_predictability(res: dict) -> list:
    """[M1] `placebo_unmatched` 추첨(밴드 없는 다른 순간)에서 사전 -> 전방 깊이.

    이 표본은 "사건이 난 종목들의, 정합 키가 정의되는(직전 60 초 막대 3 개 이상)
    순간들"이다 — 정합이 실제로 추첨하는 모집단과 같은 모집단이라 진단이 과녁에
    맞는다. **CI 없음** — 상관의 크기가 묻는 것의 전부다.
    """
    recs = []
    for key, box in sorted(res["cells"].items()):
        rtype, kind, cell, tier = key.split("|")
        if tier != "all" or (kind, cell) not in RFP.PRIMARY_CELLS:
            continue
        a = box["arms"].get("placebo_unmatched")
        if not a or not a["raw"]:
            continue
        keys = sorted(a["raw"])
        nb300 = np.concatenate([np.asarray(a["raw"][s][RFP.FWD_PROXY_KEY],
                                           "float64") for s in keys])
        nb60 = np.concatenate([np.asarray(a["raw"][s]["nbar60"], "float64")
                               for s in keys])
        fwd = np.concatenate([np.asarray(a["raw"][s][FWD_BARS], "float64")
                              for s in keys])
        sym = np.concatenate([a["raw"][s]["_symbol"] for s in keys])
        ok = np.isfinite(nb300) & np.isfinite(fwd) & (nb300 > 0)
        per_sym = []
        for u in np.unique(sym[ok]):
            m = ok & (sym == u)
            if int(m.sum()) >= MIN_DRAWS_PER_SYMBOL:
                r = spearman(nb300[m], fwd[m])
                if r is not None:
                    per_sym.append(r)
        ratio = fwd[ok] / nb300[ok]
        recs.append({
            "ranking_type": rtype, "kind": kind, "cell": cell,
            "arm": "placebo_unmatched",
            "n_draws": int(ok.sum()),
            "spearman_nbar300_fwd": spearman(nb300, fwd),
            "spearman_nbar60_fwd": spearman(nb60, fwd),
            "within_symbol": {
                "n_symbols": len(per_sym),
                "min_draws_per_symbol": MIN_DRAWS_PER_SYMBOL,
                "spearman_p25": (float(np.quantile(per_sym, 0.25))
                                 if per_sym else None),
                "spearman_p50": (float(np.median(per_sym)) if per_sym else None),
                "spearman_p75": (float(np.quantile(per_sym, 0.75))
                                 if per_sym else None),
            },
            "fwd_over_nbar300": {
                "p25": float(np.quantile(ratio, 0.25)) if ratio.size else None,
                "p50": float(np.median(ratio)) if ratio.size else None,
                "p75": float(np.quantile(ratio, 0.75)) if ratio.size else None,
            },
            "no_ci_reason": NO_CI_DIAGNOSTIC,
        })
    return recs


# --------------------------------------------------------------------------- #
# 보고
# --------------------------------------------------------------------------- #
def build_report(res: dict) -> dict:
    """산출물 하나. 문서에 실리는 수치는 전부 여기를 지나간다.

    세션 목록이 탐색 팔의 부분집합이 아니면 **여기서 죽는다** — 러너의 이중 가드
    (`ranking_forward_path.run` 의 창 상한 + 세션 상수)에 셋째 벽을 더한 것이다.
    """
    used = [s["session"] for s in res["sessions"]]
    bad = sorted(set(used) - set(RFP.EXPLORATION_SESSIONS))
    if bad:
        raise ValueError(
            f"sessions outside the exploration arm reached the report: {bad} - "
            f"G2G3-PREREG s2-1 forbids this module from ever seeing them")
    pairs = pair_table(res)
    m2 = paired_depth_response(pairs)
    m3 = gap_permutation(pairs)
    m1 = depth_predictability(res)
    return {
        "labels": list(RFP.LABELS),
        "conditions": HE.MEASUREMENT_CONDITIONS,
        "db": res["db"],
        "window": {"since_utc": res["since_utc"], "until_utc": res["until_utc"],
                   "db_max_snap_utc": res["db_max_snap_utc"]},
        "arm": {
            "name": "exploration",
            "exploration_only": bool(res["exploration_only"]),
            "sessions_allowed": list(RFP.EXPLORATION_SESSIONS),
            "sessions_used": used,
            "confirmation_floor_utc": RFP.CONFIRMATION_FLOOR_UTC,
            "no_all_sessions_switch": (
                "this module has no flag that could open the confirmation arm; "
                "it calls ranking_forward_path.run() with its default "
                "exploration-only guard and additionally dies if a session "
                "outside EXPLORATION_SESSIONS reaches the report"),
        },
        "design": {
            "question": (
                "spec w3_g2_identification s1: is the realised forward depth "
                "(bar count in the 300s after the anchor) a post-treatment "
                "quantity - does the event itself move it - so that no "
                "pre-anchor matching axis can close the realised gap in "
                "principle?"),
            "sample": (
                "the exact pairs ranking_forward_path.run() draws (same seed, "
                "same ledger) - the same pairs behind docs/65. No new draws."),
            "sign_conventions": {
                "d_fwd_bars": "real minus paired-placebo mean; POSITIVE = the "
                              "real anchor saw the deeper forward tape",
                "observed_gap": "placebo p50 minus real p50, pooled - the exact "
                                "statistic of docs/65 table [11]; POSITIVE = "
                                "placebo deeper. Note the two run in OPPOSITE "
                                "directions on purpose: each matches the table "
                                "it continues.",
            },
            "arms": {
                "primary": PRIMARY_ARM,
                "context": [a for a in MECH_ARMS if a != PRIMARY_ARM],
                "why": (
                    "in the primary arm rv60, nbar60 and nbar300 are all "
                    "band-bound WITHIN each pair, so a nonzero d_fwd cannot be "
                    "charged to pre-anchor imbalance; the context arm leaves "
                    "the nbar300 residual free and its d_fwd must be read "
                    "next to its d_nbar300 column. Chosen before the run."),
            },
            "tiers_not_opened": (
                "all-tier cells only; the tier split answers a different "
                "question and would only enlarge the corrected family. "
                "Written down before the run."),
            "decision_rules": (
                "written before the run, from the time direction of the "
                "quantities and the known cluster counts alone: "
                "(1) if the CI-bearing cells of the primary arm (only "
                "TOSS_SECURITIES_TRADING_VOLUME cells can have a pooled CI in "
                "the exploration arm - docs/65 s7) mostly EXCLUDE zero with "
                "one sign, the event moves the post-anchor tape conditional "
                "on the matched pre-state: the realised depth is "
                "post-treatment and pre-anchor matching cannot close its gap "
                "in principle - spec s1-1 resolves to (nya, alternative "
                "identification). "
                "(2) if those CIs all cross zero AND the permutation p-values "
                "are unremarkable at the Bonferroni-corrected threshold, the "
                "observed gap movements (19.5 -> 39.5 bars in docs/65 s4) are "
                "not distinguishable from noise at these pair counts and "
                "gap-closure was never a discriminating acceptance test at "
                "this sample size. "
                "(3) if the untreated Spearman of nbar300 vs forward bars is "
                "far below 1, pair-level closure of the realised depth was "
                "never an achievable matching target - only closure in "
                "expectation. "
                "(4) any other combination is written up as 'cannot be "
                "separated on this sample' (spec s6)."),
            "families": {
                "paired_ci": ("the Bonferroni denominator of [M2] is the "
                              "number of (cell, arm) records this run "
                              "produced; the runner counts it"),
                "permutation": ("[M3] p-values are printed next to "
                                "0.05 / (number of permutation records)"),
                "diagnostics": "[M1] and the untruncated subset carry no CI "
                               "and no p - they are not tests",
            },
            "bootstrap": {"n": RFP.BOOTSTRAP_N, "seed": RFP.BOOTSTRAP_SEED,
                          "cluster": "trading day (UTC session)",
                          "min_clusters": RFP.MIN_SESSION_CLUSTERS,
                          "reused_from": "ranking_forward_path - no new free "
                                         "variable"},
            "permutation": {"n": N_PERM, "seed": PERM_SEED,
                            "unit": "within-pair label exchange"},
            "costs": "NOT subtracted - that is G-3 and needs a preregistration",
        },
        "sessions": res["sessions"],
        "paired_depth_response": m2,
        "gap_permutation": m3,
        "depth_predictability": m1,
    }


# --------------------------------------------------------------------------- #
# 콘솔 - **ASCII 만.** cp949 에서 비 ASCII 는 UnicodeEncodeError 로 죽는다.
# --------------------------------------------------------------------------- #
def _f(v, nd=2):
    return "-" if v is None or not np.isfinite(float(v)) else f"{float(v):+.{nd}f}"


def _p(v):
    return "-" if v is None or not np.isfinite(float(v)) else f"{100 * float(v):5.1f}%"


def _wrap(s: str, width: int) -> list:
    out, line = [], ""
    for word in str(s).split():
        if len(line) + len(word) + 1 > width:
            out.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def print_report(rep: dict) -> None:
    """문서에 그대로 붙일 실행 출력. 요약표가 아니라 러너가 만든 수치다."""
    print("=" * 78)
    print("FORWARD-DEPTH MECHANISM - can the depth gap be closed at all?")
    print("(third dig; spec w3_g2_identification s1)")
    print("=" * 78)
    for i, s in enumerate(rep["labels"], 1):
        for j, line in enumerate(_wrap(s, 70)):
            print(f"  [{i}] {line}" if j == 0 else f"      {line}")
    arm = rep["arm"]
    print(f"  arm     : {arm['name']}  sessions used {len(arm['sessions_used'])} "
          f"{','.join(arm['sessions_used'])}")
    print(f"            floor for the reserved arm {arm['confirmation_floor_utc']}"
          f" - this module cannot read past it (no such switch exists)")
    d = rep["design"]
    print("  question:")
    for line in _wrap(d["question"], 70):
        print(f"      {line}")
    print("  decision rules (written before the run):")
    for line in _wrap(d["decision_rules"], 70):
        print(f"      {line}")
    print(f"  costs   : {d['costs']}")

    print("\n[1] sessions in window")
    print(f"{'session':<12}{'era':<12}{'reg_snaps':>10}")
    for s in rep["sessions"]:
        print(f"{s['session']:<12}{s['era']:<12}{s['regular_snaps']:>10}")

    print("\n[2] M2 - paired depth response: d_fwd = real forward bars MINUS the")
    print("    paired-placebo mean.  POSITIVE = the event anchor saw the deeper")
    print("    forward tape.  d_nbar300/d_nbar60 = the pre-anchor residual inside")
    print("    the pair (band-bound in the primary arm).  d_t2c = placebo-minus-real")
    print("    room to the close, seconds; negative = the real anchor sits closer")
    print("    to the close and its window can truncate first.  untrunc = pairs")
    print("    where neither side can truncate (point estimate only, no CI).")
    print(f"{'ranking_type':<32}{'cell':<8}{'arm':<30}{'n':>5}{'sess':>5}"
          f"{'mean':>8}{'p50':>7}{'pos%':>7}{'dn300':>7}{'dn60':>6}{'dt2c':>7}"
          f"{'n_ut':>5}{'ut_mean':>8}  ci95")
    for r in rep["paired_depth_response"]:
        x = r["diff"]
        if x["ci95"] is None:
            ci = f"NO CI (clusters {x['n_clusters']})"
        else:
            ci = (f"[{x['ci95'][0]:+.2f},{x['ci95'][1]:+.2f}]"
                  + ("  cross" if x["crosses_zero"] else "  EXCL")
                  + f" | bonf({x['n_comparisons']})"
                    f"[{x['ci_bonferroni'][0]:+.2f},{x['ci_bonferroni'][1]:+.2f}]"
                  + ("  cross" if x["crosses_zero_bonferroni"] else "  EXCL"))
        arm_tag = r["arm"].replace("placebo_vol_density", "vd") + (
            " *" if r["primary_arm"] else "")
        print(f"{r['ranking_type']:<32}{r['cell']:<8}{arm_tag:<30}"
              f"{r['n_pairs']:>5}{r['n_sessions']:>5}"
              f"{_f(r['d_fwd_bars']['mean'], 1):>8}"
              f"{_f(r['d_fwd_bars']['p50'], 1):>7}"
              f"{_p(r['d_fwd_bars']['share_positive']):>7}"
              f"{_f(r['d_nbar300_p50'], 1):>7}{_f(r['d_nbar60_p50'], 1):>6}"
              f"{_f(r['d_t_to_close_s_p50'], 0):>7}"
              f"{r['untruncated']['n']:>5}"
              f"{_f(r['untruncated']['d_fwd_mean'], 1):>8}  {ci}")
    print("    (* = primary arm; vd = placebo_vol_density)")
    print("    scales check (docs/44 s14-6): event-weighted vs symbol-uniform mean")
    for r in rep["paired_depth_response"]:
        sc = r["d_fwd_bars_scales"]
        print(f"      {r['ranking_type']:<32}{r['cell']:<8}"
              f"{r['arm'].replace('placebo_vol_density', 'vd'):<28}"
              f"ev {_f(sc['event_weighted'], 1):>8}  sym {_f(sc['symbol_uniform'], 1):>8}"
              f"  n_sym {sc['n_symbols']:>3}")

    print("\n[3] M3 - is the docs/65 [11] gap statistic information at this n?")
    print("    observed_gap = placebo p50 - real p50 (docs/65 sign).  null = the")
    print("    same statistic under within-pair label exchange (the event does")
    print("    nothing to depth).  p = two-sided, add-one.  A gap inside the null")
    print("    body says 'failed to close' was not evidence of a mechanism.")
    n_pf = len(rep["gap_permutation"])
    thr = 0.05 / max(1, n_pf)
    print(f"    permutation family = {n_pf} records -> Bonferroni threshold "
          f"{thr:.5f}")
    print(f"{'ranking_type':<32}{'cell':<8}{'arm':<30}{'n':>5}{'obs_gap':>9}"
          f"{'null|g|p50':>11}{'null|g|p90':>11}{'p_two':>9}")
    for r in rep["gap_permutation"]:
        print(f"{r['ranking_type']:<32}{r['cell']:<8}"
              f"{r['arm'].replace('placebo_vol_density', 'vd'):<30}"
              f"{r['n_pairs']:>5}{_f(r['observed_gap'], 1):>9}"
              f"{r['null_abs_gap_p50']:>11.1f}{r['null_abs_gap_p90']:>11.1f}"
              f"{r['p_two_sided']:>9.4f}")

    print("\n[4] M1 - DIAGNOSTIC, no CI: in untreated moments (unmatched placebo")
    print("    draws of the same symbols), how much of the realised forward depth")
    print("    does the pre-anchor depth pin down?  rho far below 1 = pair-level")
    print("    gap closure was never an achievable matching target.")
    print(f"{'ranking_type':<32}{'cell':<8}{'n_draws':>8}{'rho300':>8}{'rho60':>8}"
          f"{'sym_rho_p50':>12}{'n_sym':>6}{'fwd/nb300 p25/50/75':>22}")
    for r in rep["depth_predictability"]:
        w = r["within_symbol"]
        rt = r["fwd_over_nbar300"]
        ratio = (f"{rt['p25']:.2f}/{rt['p50']:.2f}/{rt['p75']:.2f}"
                 if rt["p50"] is not None else "-")
        print(f"{r['ranking_type']:<32}{r['cell']:<8}{r['n_draws']:>8,}"
              f"{_f(r['spearman_nbar300_fwd']):>8}"
              f"{_f(r['spearman_nbar60_fwd']):>8}"
              f"{_f(w['spearman_p50']):>12}{w['n_symbols']:>6}{ratio:>22}")

    print("\n" + "=" * 78)
    print("This runner decides nothing.  It reports what was measured, the sample")
    print("behind each number, and what could not be separated.")
    print("=" * 78)


def main(argv: list) -> int:
    db = HE.DB
    out_dir = RFP.OUT_DIR
    name = "forward_depth_mechanism"
    args = list(argv[1:])
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif a == "--name":
            name = args[i + 1]; i += 2
        else:
            db = Path(a); i += 1
    print("running ranking_forward_path.run() on the exploration arm "
          "(same draws as docs/65) ...", flush=True)
    # 확증 팔을 여는 인자는 **존재하지 않는다.** 기본값이 곧 유일한 모드다.
    res = RFP.run(db, progress=True)
    rep = build_report(res)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print_report(rep)
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""**초 안 순서 의존의 파급범위** (docs/42) — 어디가 오염됐고, 얼마나, 어느 쪽으로.

## 무엇이 일어났는가 (docs/41 §2-1)

`trades_snap` 은 `WITHOUT ROWID`, PK `(symbol, ts_ms, price_u, qty_u)` 다.
그래서 `ORDER BY symbol, ts_ms` 는 같은 초 안을 **가격 오름차순**으로 돌려준다.
그 순서를 시간 순서로 읽으면 초 안에서 **가격은 언제나 오르기만 한다.**

**이건 정렬을 바꿔 고칠 버그가 아니다.** `ts_ms` 가 전부 `.000` 이라 초 안의 순서라는
정보가 **데이터에 애초에 없다.** 폴링을 빨리 해도 안 내려간다 — 서버가 초 단위로 준다.
유일하게 정직한 표현은 **초 단위 집계**다.

## 이 모듈이 하는 일 — **재판정이 아니다**

1. **목록** (`INVENTORY`): 체결 순서를 쓰는 모든 지점을 전수로 세고
   **(가) 순서 무관 / (나) 의존 → 오염 / (다) 판정 불가** 로 가른다.
2. **크기와 방향**: 오염의 실제 크기를 두 곳에서 잰다 —
   틱 매수/매도 판정(`M1`)과 창 수익률의 `first_px`/`last_px`(`M2`).
   방법은 **같은 함수를 두 번 돌리는 것**이다: 저장 순서 그대로 vs **초 안을 섞은 대조군**.
   차이가 곧 저장 순서가 만든 몫이다.

> **`docs/28`·`docs/29` 의 수치를 재계산해 "새 값은 이것" 이라고 쓰지 않는다.**
> 여기서 나가는 것은 **편향의 크기와 부호**뿐이다. 재판정은 사용자와 함께 본다.

실행: `python -m tossmon.analysis.measure.intra_second_bias [db_path]`
라이브 0콜. DB 는 `mode=ro`.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

from tossmon.analysis.measure.tick_resolution import (
    OUT_DIR, WINDOW_START_MS, open_ro, pct_table,
)

#: `docs/28` 계측기가 쓰는 창. 같은 창에서 편향을 재야 크기가 비교된다.
WINDOWS_S = (5, 10, 30, 60)

#: 편향 측정에 쓸 종목 하한 (체결 건수).
MIN_TRADES = 500

SEED = 20260807

#: 분류 기호.
K_SAFE = "가"          # 순서 무관 — 초 안을 어떻게 섞어도 값이 같다
K_DIRTY = "나"         # 순서 의존 → 오염. 크기·방향을 잴 수 있다
K_UNKNOWN = "다"       # 판정 불가 — 초 안 정보가 있어야 답할 수 있는 질문

#: ★ 전수 목록. **`다` 를 `가` 에 넣지 않는다.**
#: `(module, attr)` 는 테스트가 실제 존재를 확인한다 — 목록이 코드에서 떨어져 썩지 않게.
INVENTORY: tuple[dict, ...] = (
    # --- 원천 --------------------------------------------------------------
    {"module": "tossmon.analysis.measure.tick_instrument", "attr": "load_ticks",
     "output": "ticks 프레임(행 순서)", "klass": K_DIRTY,
     "why": "ORDER BY symbol, ts_ms — 여기서 가격 오름차순이 들어온다. 모든 하류의 근원."},
    {"module": "tossmon.analysis.measure.tick_instrument", "attr": "tick_classify",
     "output": "side_tick", "klass": K_DIRTY,
     "why": "직전 행과의 가격 비교. 초 안에서는 직전 행이 항상 더 싸다 → 전부 매수."},
    {"module": "tossmon.analysis.measure.tick_instrument", "attr": "quote_classify",
     "output": "side_quote", "klass": K_SAFE,
     "why": "체결가를 호가 중간값과 비교한다. 이웃 행을 안 본다."},
    {"module": "tossmon.analysis.measure.tape_cost", "attr": "load_tape",
     "output": "tape 프레임(행 순서)", "klass": K_DIRTY,
     "why": "같은 ORDER BY. 다만 하류가 순서를 안 쓴다(아래 align_trades_to_l1)."},

    # --- 초 안 순서를 실제로 소비하는 지점 -----------------------------------
    {"module": "tossmon.analysis.measure.tick_instrument", "attr": "window_frame",
     "output": "first_px / last_px / ret_bp / fwd_ret_bp", "klass": K_DIRTY,
     "why": "first=창 첫 초의 **최저가**, last=창 끝 초의 **최고가** → 수익률이 위로 밀린다."},
    {"module": "tossmon.analysis.measure.tick_instrument", "attr": "window_frame",
     "output": "n_buy / n_sell / buy_share / imbalance", "klass": K_DIRTY,
     "why": "side_tick 을 센다."},
    {"module": "tossmon.analysis.measure.tick_instrument", "attr": "window_frame",
     "output": "n_trades / volume / trades_per_s / volume_per_s", "klass": K_SAFE,
     "why": "합·개수는 순서와 무관하다. (단 중복 접힘은 별개 문제 — docs/41 §2-2)"},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "classifier_agreement", "output": "agreement (틱 vs 호가 일치율)",
     "klass": K_DIRTY, "why": "side_tick 이 한쪽 항이다."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "classifier_agreement", "output": "moving / price_flat 구분",
     "klass": K_DIRTY,
     "why": "직전 **행** 과의 가격 비교라 붐비는 초는 거의 전부 moving 으로 간다."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "flow_price_response", "output": "same/next window ret by imbalance bucket",
     "klass": K_DIRTY, "why": "imbalance 로 나누고 ret_bp 를 본다 — 양쪽 다 오염."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "window_stability", "output": "top/bottom bucket spread",
     "klass": K_DIRTY, "why": "flow_price_response 의 요약."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "random_time_control", "output": "corr(real) vs corr(shuffled)",
     "klass": K_DIRTY,
     "why": "대조군은 **창 사이**를 섞는다. 시간 짝을 끊는 귀무가설에는 제대로 답하지만, "
            "두 팔이 **같은 오염된 imbalance** 를 쓰므로 '이 변수 자체가 정렬의 산물인가' "
            "는 원리상 못 묻는다. 이 대조군은 그 질문을 위해 만든 것이 아니다."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "intensity_table", "output": "trades_per_s / volume_per_s",
     "klass": K_SAFE, "why": "건수·거래량 합계."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "intensity_table", "output": "buy_share_median / abs_imbalance_median",
     "klass": K_DIRTY, "why": "같은 표 안에 있지만 side_tick 에서 온다. 한 표라고 "
                              "한 등급이 아니다."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "window_occupancy", "output": "trades_per_window / share_ge_N",
     "klass": K_SAFE, "why": "행 개수."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "window_occupancy", "output": "imbalance_saturated_share",
     "klass": K_DIRTY, "why": "imbalance 포화 비율 — side_tick 에서 온다."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "symbol_day_inventory", "output": "종목-일 체결 건수", "klass": K_SAFE,
     "why": "개수."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "usable_symbol_days", "output": "표본 충분성", "klass": K_SAFE,
     "why": "개수 임계."},
    {"module": "tossmon.analysis.measure.tick_instrument",
     "attr": "days_needed_for_symbol_days", "output": "필요 사이클 수", "klass": K_SAFE,
     "why": "개수에서만 나온다."},

    # --- docs/29 (맛보기) ---------------------------------------------------
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "top_decile_symbols",
     "output": "상위 10분위 종목", "klass": K_SAFE, "why": "체결 건수 분위."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "second_scale_panel",
     "output": "intensity_change", "klass": K_SAFE,
     "why": "n_trades 비율. 다만 같은 프레임의 다른 열은 오염돼 있다."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "leadlag_table",
     "output": "선행/후행 상관", "klass": K_DIRTY,
     "why": "신호가 imbalance 이고 응답이 체결가 기반이다."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "asymmetry",
     "output": "비대칭과 잡음 폭", "klass": K_DIRTY, "why": "leadlag_table 의 차분."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "find_shot_starts",
     "output": "start_px / peak_px / duration_s / total_rise", "klass": K_DIRTY,
     "why": "행 단위로 저점→고점을 훑는다. 초 안에서 저점=그 초 최저, 고점=그 초 최고라 "
            "상승은 부풀고 지속시간은 짧아진다. docs/41 §6 이 그 크기를 보인다."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "detection_profile",
     "output": "imbalance_by_offset_median", "klass": K_DIRTY, "why": "side_tick."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "detection_profile",
     "output": "ceiling_remaining_rise", "klass": K_DIRTY,
     "why": "px[b] 가 그 초의 **최고가**라 '남은 상승'이 아래로 밀린다."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "detection_profile",
     "output": "trades_by_offset_median / share_still_in_progress", "klass": K_DIRTY,
     "why": "계산 자체는 순서 무관이지만 **입력인 슈팅 경계(start_ms·peak_ms)가 "
            "find_shot_starts 에서 온다.** 오염을 물려받는다."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "shot_threshold_sweep",
     "output": "임계별 슈팅 수·지속·상한", "klass": K_DIRTY,
     "why": "find_shot_starts 의 요약."},
    {"module": "tossmon.analysis.measure.tick_tasting", "attr": "data_needed",
     "output": "필요 사이클 수", "klass": K_DIRTY,
     "why": "슈팅 **개수**만 쓰지만 그 개수가 find_shot_starts 에서 온다."},

    # --- 순서를 안 쓰는 소비자들 --------------------------------------------
    {"module": "tossmon.analysis.measure.tape_cost", "attr": "align_trades_to_l1",
     "output": "체결별 L1 정렬·충격", "klass": K_SAFE,
     "why": "행마다 독립적으로 searchsorted 로 직전 호가를 붙인다. 이웃 행을 안 본다."},
    {"module": "tossmon.analysis.measure.exit_value", "attr": "load_day_tape",
     "output": "일별 테이프(수량)", "klass": K_SAFE,
     "why": "price_u 를 안 읽고 수량만 집계한다."},

    # --- (다) 판정 불가 -----------------------------------------------------
    {"module": None, "attr": None,
     "output": "초 안의 진짜 매수/매도 방향", "klass": K_UNKNOWN,
     "why": "초 안 순서가 데이터에 없다. 호가는 4초 주기라 초 안을 못 가른다."},
    {"module": None, "attr": None,
     "output": "1초보다 짧은 슈팅의 지속시간", "klass": K_UNKNOWN,
     "why": "눈금이 1초다. docs/41 §6-2: 전체 초의 11.3%가 그 초 안에서 이미 1% 폭이다."},
    {"module": None, "attr": None,
     "output": "초 안 체결 건수의 참값", "klass": K_UNKNOWN,
     "why": "(ts, price, qty) 동일 체결이 접힌다(docs/41 §2-2). 접힌 수를 아무도 안 센다."},
    {"module": None, "attr": None,
     "output": "포화된 4초 칸 안에서 실제로 일어난 일", "klass": K_UNKNOWN,
     "why": "50건 상한에 잘려 응답에 안 실렸다. 지연이 아니라 결손이다(docs/41 §4)."},
)


def inventory_counts() -> dict:
    out = {K_SAFE: 0, K_DIRTY: 0, K_UNKNOWN: 0}
    for row in INVENTORY:
        out[row["klass"]] += 1
    return out


# --------------------------------------------------------------------------- #
# 공통 — 초 안을 섞은 대조군
# --------------------------------------------------------------------------- #
def _symbols(conn: sqlite3.Connection, min_trades: int = MIN_TRADES) -> list[str]:
    return [s for s, in conn.execute(
        "SELECT symbol FROM trades_snap WHERE ts_ms >= ? GROUP BY symbol "
        "HAVING COUNT(*) >= ?", (WINDOW_START_MS, min_trades))]


def _stored_order(conn: sqlite3.Connection, symbol: str):
    """`load_ticks` 와 **같은 방식으로** 읽는다 — 저장 순서를 그대로 재현한다."""
    rows = conn.execute(
        "SELECT ts_ms, price_u, qty_u FROM trades_snap "
        "WHERE symbol = ? AND ts_ms >= ? ORDER BY symbol, ts_ms",
        (symbol, WINDOW_START_MS)).fetchall()
    ts = np.asarray([r[0] for r in rows], dtype="int64")
    px = np.asarray([r[1] for r in rows], dtype="float64")
    qty = np.asarray([r[2] for r in rows], dtype="float64")
    return ts, px, qty


def _shuffle_within_second(ts: np.ndarray, rng) -> np.ndarray:
    """초 경계는 그대로 두고 **안쪽만** 섞는 인덱스."""
    return np.lexsort((rng.random(len(ts)), ts))


# --------------------------------------------------------------------------- #
# M1. 틱 매수/매도 판정 — 크기와 방향
# --------------------------------------------------------------------------- #
def tick_rule_bias(conn: sqlite3.Connection, *, min_trades: int = MIN_TRADES,
                   seed: int = SEED) -> dict:
    """`tick_instrument.tick_classify` 를 **저장 순서**와 **초 안 섞기**로 각각 돌린다.

    같은 함수·같은 행·같은 초다. 다른 것은 초 안의 배열뿐이므로,
    두 결과의 차이는 **전부 저장 순서가 만든 것**이다.
    """
    TI = importlib.import_module("tossmon.analysis.measure.tick_instrument")
    rng = np.random.default_rng(seed)
    tot = 0
    cnt = {"stored": [0, 0, 0], "shuffled": [0, 0, 0]}     # [buy, sell, unclassified]
    differ = 0
    for sym in _symbols(conn, min_trades):
        ts, px, _q = _stored_order(conn, sym)
        if ts.size < 2:
            continue
        a = TI.tick_classify(px)
        idx = _shuffle_within_second(ts, rng)
        b = TI.tick_classify(px[idx])
        tot += ts.size
        for name, v in (("stored", a), ("shuffled", b)):
            cnt[name][0] += int((v > 0).sum())
            cnt[name][1] += int((v < 0).sum())
            cnt[name][2] += int((v == 0).sum())
        # 같은 행끼리 비교하려면 섞은 결과를 원래 자리로 되돌린다
        back = np.empty_like(b)
        back[idx] = b
        differ += int((a != back).sum())

    def _share(v):
        return {"buy": v[0] / tot, "sell": v[1] / tot, "unclassified": v[2] / tot} \
            if tot else {}

    st, sh = _share(cnt["stored"]), _share(cnt["shuffled"])
    return {
        "n_trades": tot,
        "min_trades_per_symbol": min_trades,
        "stored_order": st,
        "shuffled_control": sh,
        "rows_classified_differently_share": (differ / tot) if tot else 0.0,
        "buy_share_excess": (st.get("buy", 0.0) - sh.get("buy", 0.0)) if tot else None,
        "direction": ("저장 순서 쪽 매수 비율이 더 높으면 틱 규칙은 **매수를 과대**로 "
                      "읽는다 — 초 안이 가격 오름차순이기 때문이다."),
        "note": ("`tick_classify` 를 **고치지 않고 그대로 호출**했다. 이 표는 그 함수의 "
                 "결과가 입력 배열의 순서에 얼마나 좌우되는지만 말한다."),
    }


# --------------------------------------------------------------------------- #
# M2. 창 수익률 — first_px / last_px 편향
# --------------------------------------------------------------------------- #
def window_return_bias(conn: sqlite3.Connection, *, windows_s=WINDOWS_S,
                       min_trades: int = MIN_TRADES, seed: int = SEED) -> dict:
    """`window_frame` 의 `ret_bp = log(last_px / first_px)` 를 두 순서로 만든다.

    저장 순서에서 `first_px` 는 창 첫 초의 **최저가**, `last_px` 는 창 끝 초의
    **최고가**다. 그래서 창이 실제로 횡보해도 수익률이 **양수로** 나온다.
    """
    rng = np.random.default_rng(seed)
    out = {}
    syms = _symbols(conn, min_trades)
    for w in windows_s:
        stored_r, shuf_r = [], []
        for sym in syms:
            ts, px, _q = _stored_order(conn, sym)
            if ts.size < 2:
                continue
            bucket = (ts // (w * 1000)) * (w * 1000)
            idx = _shuffle_within_second(ts, rng)
            for order_px, order_bucket, sink in (
                    (px, bucket, stored_r), (px[idx], bucket[idx], shuf_r)):
                # 각 버킷의 첫 행 / 마지막 행 (pandas first/last 와 같은 의미)
                edges = np.nonzero(np.diff(order_bucket) != 0)[0]
                starts = np.concatenate(([0], edges + 1))
                ends = np.concatenate((edges, [len(order_bucket) - 1]))
                f = order_px[starts]
                l = order_px[ends]
                ok = (f > 0) & (l > 0) & (ends > starts)
                if ok.any():
                    sink.append(np.log(l[ok] / f[ok]) * 1e4)
        s = np.concatenate(stored_r) if stored_r else np.array([])
        h = np.concatenate(shuf_r) if shuf_r else np.array([])
        out[f"{w}s"] = {
            "stored_ret_bp": pct_table(s),
            "shuffled_ret_bp": pct_table(h),
            "mean_excess_bp": (float(s.mean() - h.mean())
                               if s.size and h.size else None),
            "stored_positive_share": float((s > 0).mean()) if s.size else None,
            "shuffled_positive_share": float((h > 0).mean()) if h.size else None,
        }
    return {"min_trades_per_symbol": min_trades, "by_window": out,
            "reading": ("저장 순서 쪽 평균이 위로 치우쳐 있으면 그만큼이 정렬이 만든 "
                        "가짜 상승이다. 창이 길수록 창 안의 초가 많아져 첫·끝 초의 "
                        "비중이 줄고 편향도 옅어진다.")}


# --------------------------------------------------------------------------- #
# 노출도 — 어느 시대까지 미치는가
# --------------------------------------------------------------------------- #
def exposure_by_era(conn: sqlite3.Connection) -> dict:
    """편향은 **저장 방식**에서 오므로 스키마가 안 바뀐 전 구간에 걸린다.

    시대별로 "한 초에 두 건 이상" 인 행의 비율을 낸다 — 그게 노출 규모다.
    """
    rows = conn.execute(
        """SELECT CASE WHEN ts_ms < ? THEN 'pre_0804' ELSE 'from_0804' END era,
                  ts_ms, symbol, COUNT(*) c
           FROM trades_snap GROUP BY symbol, ts_ms""", (WINDOW_START_MS,)).fetchall()
    agg: dict[str, list[int]] = {}
    for era, _ts, _sym, c in rows:
        a = agg.setdefault(era, [0, 0, 0])
        a[0] += c                                   # 전체 행
        a[1] += 1                                   # (종목, 초) 묶음
        if c >= 2:
            a[2] += c                               # 다건 초 안의 행
    return {era: {"rows": a[0], "symbol_seconds": a[1],
                  "rows_in_multi_trade_seconds": a[2],
                  "share": (a[2] / a[0]) if a[0] else 0.0}
            for era, a in sorted(agg.items())}


# --------------------------------------------------------------------------- #
# 조립
# --------------------------------------------------------------------------- #
def build_report(conn: sqlite3.Connection) -> dict:
    return {
        "inventory": list(INVENTORY),
        "inventory_counts": inventory_counts(),
        "tick_rule_bias": tick_rule_bias(conn),
        "window_return_bias": window_return_bias(conn),
        "exposure_by_era": exposure_by_era(conn),
        "not_a_verdict": ("docs/28·docs/29 의 수치를 다시 계산하지 않았다. "
                          "여기 있는 것은 편향의 크기와 부호뿐이다."),
    }


def main(db: Path, *, out_dir: Path | None = None) -> int:
    conn = open_ro(db)
    rep = build_report(conn)

    c = rep["inventory_counts"]
    print("=== [docs/42 sec 1] INVENTORY OF ORDER-DEPENDENT POINTS")
    print(f"  (가) order-independent {c[K_SAFE]}   "
          f"(나) contaminated {c[K_DIRTY]}   (다) undecidable {c[K_UNKNOWN]}")
    for k in (K_DIRTY, K_UNKNOWN, K_SAFE):
        print(f"  --- ({k})")
        for r in rep["inventory"]:
            if r["klass"] != k:
                continue
            where = f"{r['module'].split('.')[-1]}.{r['attr']}" if r["module"] else "-"
            print(f"    {where:<34} {r['output']}")

    t = rep["tick_rule_bias"]
    print("\n=== [docs/42 sec 2] M1 TICK RULE - SIZE AND DIRECTION")
    print(f"  trades {t['n_trades']}")
    print(f"  stored order    buy {t['stored_order']['buy']:.4f}  "
          f"sell {t['stored_order']['sell']:.4f}  "
          f"unclassified {t['stored_order']['unclassified']:.4f}")
    print(f"  shuffled control buy {t['shuffled_control']['buy']:.4f}  "
          f"sell {t['shuffled_control']['sell']:.4f}  "
          f"unclassified {t['shuffled_control']['unclassified']:.4f}")
    print(f"  buy share excess {t['buy_share_excess']:+.4f}   "
          f"rows classified differently {t['rows_classified_differently_share']:.4f}")

    print("\n=== [docs/42 sec 3] M2 WINDOW RETURN (first_px/last_px)")
    for w, v in rep["window_return_bias"]["by_window"].items():
        print(f"  {w:>4}  stored mean {v['stored_ret_bp']['mean']:+8.2f} bp  "
              f"shuffled mean {v['shuffled_ret_bp']['mean']:+8.2f} bp  "
              f"excess {v['mean_excess_bp']:+8.2f} bp   "
              f"positive share {v['stored_positive_share']:.3f} vs "
              f"{v['shuffled_positive_share']:.3f}  n={v['stored_ret_bp']['n']}")

    print("\n=== [docs/42 sec 4] EXPOSURE BY ERA")
    for era, v in rep["exposure_by_era"].items():
        print(f"  {era:<10} rows {v['rows']:>8}  in multi-trade seconds "
              f"{v['share']:.3f}")

    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "intra_second_bias.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db")))

"""운영 텔레메트리로 **게이트 판정을 오프라인 재생**한다 (docs/46 §5·§6·§7).

라이브 호출 0. 로그만 읽는다 (읽기 전용).

무엇을 재는가
-------------
`collector.log` 의 telemetry 줄에는 표본마다 MARKET_DATA 의 `peak_1s` 와 `measured_rate`
(`avg`) 가 둘 다 남아 있다. 두 게이트는 그 중 `peak_1s` 만 보고 있었다:

    정원 복원 (budget.should_grow)   차단  iff  peak_1s > target × 0.70
    tier2 호가 게이트 (loops)        닫힘  iff  peak_1s >= target × 0.90

같은 표본에 **지속률 기준**을 대면 몇 %가 열렸을지가 이 스크립트의 답이다.

★ 이 숫자를 어떻게 읽어야 하는가 (안 읽으면 틀리게 읽는다)
--------------------------------------------------------
로그의 `peak` 도 `avg` 도 **이중 계상된 값**이다 (docs/45 D1·D2). 다만 둘의 성격이 다르다:

  * `peak` 는 **분명히 부풀었다.** MARKET_DATA 가 1.15초 하드캡 아래에서 낼 수 있는
    1초 최댓값은 10 인데 로그에는 11~14 가 181개 표본에 있다. 그 초과는 전부 남의
    송신이다.
  * `avg` 는 **부호를 모른다.** 훔쳐온 송신은 늘리고(D1), `after_call` 의 옛 클램프가
    버린 몫(`budget_unattributed_attempts` 171,905)은 줄인다. 어느 쪽이 큰지 이 로그로는
    못 가른다. **그래서 "avg 는 상한이다" 라고 쓰면 안 된다.**

`avg` 대신 **설정 산술**로 상계를 잡는다: tier3 정원이 꽉 찬 10, tier2 호가 게이트가
완전히 열린 상태에서 MARKET_DATA 의 지속 부하는

    tier1 8배치/45s + tier3 체결 10/4s + tier3 호가 10/4s + tier2 호가 300/600s = 5.68 req/s

이고, 이것은 정원 복원 문턱(8.5×0.70 = 5.95)과 tier2 게이트 문턱(8.5×0.90 = 7.65)
**둘 다보다 작다.** 즉 계상이 옳다면 현행 설정에서 지속률이 두 게이트를 트립시킬 수 없다.
이 결론은 로그의 편향과 무관하다 (`tools/cadence_budget.py` 가 코드로 재계산한다).

여전히 못 재는 것: **고쳐진 계상에서 `peak` 와 `avg` 가 실제로 얼마가 되는지.**
그것은 배포 후 관측해야 나온다. 아래 "새 규칙" 열은 옛 표본에 새 기준을 댄 재생일 뿐
새 계상의 실측이 아니다.

재실행:
    python -m tools.replay_gating --log ../w5-ops/data/collector.log
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

# telemetry 줄의 `| budget MARKET_DATA=peak11/p95:8/avg4.93/tgt8.50` 꼬리.
RE_MD = re.compile(r"MARKET_DATA=peak(?P<peak>\d+)/p95:(?P<p95>[\d.]+)"
                   r"/avg(?P<avg>[\d.]+)/tgt(?P<tgt>[\d.]+)")
RE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

#: 판정 상수 — `budget.py` / `loops.py` 와 같은 값이어야 한다.
RECOVER_USAGE_MAX = 0.70
TIER2_HEADROOM = 0.90
MD_LIMIT = 10.0

FIELDS = ("session", "md_peak_1s", "tier2", "tier3", "tier2_cap", "tier3_cap",
          "tier2_orderbook_snaps", "tier2_orderbook_skipped", "over_limit_1s",
          "http_429", "budget_shrinks", "budget_restores", "config_sig")


def parse(log: Path) -> list[dict]:
    out: list[dict] = []
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if " telemetry " not in line or "md_peak_1s=" not in line:
                continue
            md = RE_MD.search(line)
            ts = RE_TS.match(line)
            if not md or not ts:
                continue
            row: dict = {"ts": ts.group("ts"),
                         "peak": float(md.group("peak")),
                         "avg": float(md.group("avg")),
                         "tgt": float(md.group("tgt"))}
            for name in FIELDS:
                m = re.search(rf"\b{name}=(\S+)", line)
                if m:
                    row[name] = m.group(1)
            out.append(row)
    return out


def _num(row: dict, key: str) -> int:
    try:
        return int(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _deltas(rows: list[dict], key: str) -> list[int]:
    """누적 카운터의 표본 간 증가분. **재기동(감소)은 버린다** — 음수를 0 으로 접으면
    재기동 직전의 진짜 증가분까지 사라져 총량이 조용히 줄어든다."""
    out: list[int] = []
    prev = None
    for row in rows:
        cur = _num(row, key)
        if prev is not None:
            out.append(cur - prev if cur >= prev else 0)
        prev = cur
    return out


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def _collecting_seconds(rows: list[dict]) -> float:
    """세션이 열려 있던(=tier2 호가 루프가 도는) 구간의 경과 초.

    표본 간격(300초)을 그 구간의 길이로 본다. `closed` 세션과 300초를 크게 넘는
    간격(= 수집기 정지)은 뺀다 — 안 빼면 분모가 부풀어 req/s 가 과소평가된다.
    """
    from datetime import datetime

    total = 0.0
    prev_t = None
    for row in rows:
        t = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
        if prev_t is not None:
            gap = (t - prev_t).total_seconds()
            if gap <= 400 and row.get("session") not in (None, "closed"):
                total += gap
        prev_t = t
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default="../w5-ops/data/collector.log")
    args = ap.parse_args()

    rows = parse(Path(args.log))
    if not rows:
        raise SystemExit("telemetry 표본이 없다 — 로그 경로를 확인하라")
    n = len(rows)
    print(f"# 표본 {n}개  {rows[0]['ts']} ~ {rows[-1]['ts']}")
    sigs = {r.get("config_sig", "?") for r in rows}
    print(f"# config_sig {len(sigs)}종 (MARKET_DATA 폴링 부분은 동일: "
          "t3max10,tr4s,ob4s,t2ob600s)")
    print()

    # ---- 게이트 재생 -----------------------------------------------------
    old_grow_blocked = sum(1 for r in rows if r["peak"] > r["tgt"] * RECOVER_USAGE_MAX)
    new_grow_blocked = sum(1 for r in rows
                           if r["avg"] > r["tgt"] * RECOVER_USAGE_MAX
                           or r["peak"] > MD_LIMIT)
    old_t2_closed = sum(1 for r in rows if r["peak"] >= r["tgt"] * TIER2_HEADROOM)
    new_t2_closed = sum(1 for r in rows
                        if r["avg"] >= r["tgt"] * TIER2_HEADROOM or r["peak"] > MD_LIMIT)

    print("## 게이트 (같은 표본, 판정 기준만 교체)")
    print(f"{'':34} {'옛 규칙(peak vs 지속목표)':>26} {'새 규칙(지속률 + 1초한도)':>28}")
    print(f"{'정원 복원이 막힌 표본':34} {pct(old_grow_blocked, n):>26} "
          f"{pct(new_grow_blocked, n):>28}")
    print(f"{'tier2 호가 게이트가 닫힌 표본':34} {pct(old_t2_closed, n):>26} "
          f"{pct(new_t2_closed, n):>28}")
    print()

    # 새 규칙에서 닫히는 이유를 갈라 본다 — "1초 한도 초과" 는 계상이 고쳐지면 사라진다.
    by_limit = sum(1 for r in rows if r["peak"] > MD_LIMIT)
    by_rate = sum(1 for r in rows if r["avg"] >= r["tgt"] * TIER2_HEADROOM)
    print(f"  새 규칙에서 닫히는 사유: 지속률 {pct(by_rate, n)} / "
          f"1초 한도 초과 {pct(by_limit, n)} (후자는 이중 계상의 잔재다 — 하드캡 아래에서")
    print("  MARKET_DATA 가 낼 수 있는 1초 최댓값은 10 이므로 11~14 는 남의 송신이다)")
    hi = max(r["avg"] for r in rows)
    print(f"  관측된 avg 최댓값 {hi:.2f} req/s — 복원 문턱 "
          f"{rows[-1]['tgt'] * RECOVER_USAGE_MAX:.2f}, tier2 문턱 "
          f"{rows[-1]['tgt'] * TIER2_HEADROOM:.2f} 을 **한 표본도** 넘지 않았다")
    print()

    # ---- 세션별 -----------------------------------------------------------
    print("## 세션별 (tier2 호가 게이트)")
    print(f"{'세션':10} {'표본':>6} {'옛 규칙 닫힘':>12} {'새 규칙 닫힘':>12} "
          f"{'평균 avg':>9} {'평균 peak':>10}")
    for sess in ("regular", "pre", "after", "day", "closed"):
        sub = [r for r in rows if r.get("session") == sess]
        if not sub:
            continue
        o = sum(1 for r in sub if r["peak"] >= r["tgt"] * TIER2_HEADROOM)
        w = sum(1 for r in sub if r["avg"] >= r["tgt"] * TIER2_HEADROOM
                or r["peak"] > MD_LIMIT)
        print(f"{sess:10} {len(sub):>6} {pct(o, len(sub)):>12} {pct(w, len(sub)):>12} "
              f"{sum(r['avg'] for r in sub) / len(sub):>9.2f} "
              f"{sum(r['peak'] for r in sub) / len(sub):>10.2f}")
    print()

    # ---- tier2 호가 실수율 ------------------------------------------------
    snaps = sum(_deltas(rows, "tier2_orderbook_snaps"))
    skips = sum(_deltas(rows, "tier2_orderbook_skipped"))
    tries = snaps + skips
    print("## tier2 호가 — 실제로 얼마나 나갔나 (관측 창 안의 증가분)")
    print(f"  성공 {snaps:,}건 / 양보 {skips:,}건 / 시도 {tries:,}건  "
          f"→ 수율 {pct(snaps, tries)}")
    # 게이트가 열리면 이만큼이 **MARKET_DATA 예산으로 돌아온다.** 폴 주기 결정의 입력이다.
    open_s = _collecting_seconds(rows)
    if open_s > 0:
        got = snaps / open_s
        want = tries / open_s
        print(f"  수집 중 경과 {open_s / 3600:.1f}h 기준: 실제 {got:.3f} req/s / "
              f"시도 {want:.3f} req/s → 게이트가 열리면 **+{want - got:.3f} req/s**")
    print()

    # ---- 정원이 천장 아래에 있던 시간 -------------------------------------
    below2 = sum(1 for r in rows if 0 < _num(r, "tier2_cap") < 300)
    below3 = sum(1 for r in rows if 0 < _num(r, "tier3_cap") < 10)
    print("## 정원이 세션 천장 아래에 있던 표본")
    print(f"  tier2_cap < 300: {pct(below2, n)}   tier3_cap < 10: {pct(below3, n)}")
    print()

    # ---- 정원 래칫 --------------------------------------------------------
    caps2 = [_num(r, "tier2_cap") for r in rows if "tier2_cap" in r]
    caps3 = [_num(r, "tier3_cap") for r in rows if "tier3_cap" in r]
    if caps2:
        print("## 정원 (래칫 관측)")
        print(f"  tier2_cap  최소 {min(caps2)} / 중앙 {sorted(caps2)[len(caps2)//2]} "
              f"/ 최대 {max(caps2)}")
        print(f"  tier3_cap  최소 {min(caps3)} / 중앙 {sorted(caps3)[len(caps3)//2]} "
              f"/ 최대 {max(caps3)}")
        shrinks = sum(_deltas(rows, "budget_shrinks"))
        restores = sum(_deltas(rows, "budget_restores"))
        print(f"  축소 {shrinks:,}회 / 복원 {restores:,}회")
    print()

    # ---- 정원 × 첨두 ------------------------------------------------------
    # **복원이 왜 영영 안 열렸는지**가 여기서 보인다. tier3 루프는 만기 항목을 연달아
    # 쏘므로 한 배치가 `2 × tier3_cap` 건이고, 그것이 그대로 `peak_1s` 가 된다.
    # 복원 문턱은 target × 0.70 = 5.95 이므로 **tier3 정원이 3 이상이면 첨두가 6 이상**이라
    # 문턱을 넘는다 — 정원이 올라가는 순간 복원이 스스로 잠긴다.
    from collections import defaultdict
    by_cap: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        by_cap[_num(r, "tier3_cap")].append(r["peak"])
    # 정원이 오른 경로를 갈라 센다: 게이트를 통과한 진짜 복원인가, 세션 전환 리셋인가.
    same = diff = same_amt = diff_amt = 0
    prev = None
    for r in rows:
        cur = (_num(r, "tier3_cap"), r.get("session"))
        if prev is not None and cur[0] > prev[0]:
            if cur[1] == prev[1]:
                same += 1
                same_amt += cur[0] - prev[0]
            else:
                diff += 1
                diff_amt += cur[0] - prev[0]
        prev = cur
    print("## tier3 정원이 오른 경로")
    print(f"  같은 세션 안 (게이트 통과) {same}회 +{same_amt}종목  /  "
          f"세션 전환 리셋 {diff}회 +{diff_amt}종목")
    print()

    print("## tier3 정원별 MARKET_DATA 첨두 (복원이 왜 안 열렸는가)")
    print(f"{'tier3_cap':>9} {'표본':>6} {'peak 중앙':>10} {'peak 최소':>10} "
          f"{'peak 최대':>10}  복원 문턱 5.95 대비")
    for cap in sorted(by_cap):
        v = sorted(by_cap[cap])
        med = v[len(v) // 2]
        # "중앙이 문턱 위" 는 **평상시 불만족**이라는 뜻이지 "항상 막힘" 이 아니다.
        # 첨두는 표본 사이에서 출렁이고(peak 최소 열), 복원은 그 틈으로 나갔다.
        mark = "중앙이 문턱 위" if med > 5.95 else "중앙이 문턱 아래"
        print(f"{cap:>9} {len(v):>6} {med:>10.0f} {v[0]:>10.0f} {v[-1]:>10.0f}  {mark}")
    print()

    # ---- 첨두 분포 --------------------------------------------------------
    over = sum(1 for r in rows if r["peak"] > MD_LIMIT)
    print("## MARKET_DATA 첨두 분포 (전부 이중 계상된 값)")
    print(f"  peak > 한도 10 인 표본: {over} / {n} = {pct(over, n)}")
    hist: dict[int, int] = {}
    for r in rows:
        hist[int(r["peak"])] = hist.get(int(r["peak"]), 0) + 1
    print("  " + "  ".join(f"{k}:{hist[k]}" for k in sorted(hist)))
    print(f"  평균 avg {sum(r['avg'] for r in rows) / n:.2f} req/s "
          f"(목표 {rows[-1]['tgt']:.2f}) — 부호를 모르는 값이다 (모듈 docstring ★)")
    print(f"  참고: tier3 정원이 꽉 찼을 때의 구조적 지속 부하는 5.68 req/s "
          "(tools/cadence_budget.py) — 관측 평균이 그보다 낮은 이유는 정원이 "
          f"천장 아래에 있던 시간이 {pct(below3, n)} 이기 때문이다")


if __name__ == "__main__":
    main()

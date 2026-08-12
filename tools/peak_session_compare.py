"""정규장 첨두를 **세션 대 세션**으로 비교한다 (docs/52 §5.5 시계 수정 검증).

라이브 호출 0. 수집기 건드리지 않는다. `collector.log` 는 **읽기 전용**.

왜 세션을 맞추는가
------------------
아침(after 세션)과 밤(regular 세션)은 **부하가 다르다**. 감시 종목 수도, 체결
빈도도, 폴링 대상도 다르다. 그 둘을 나란히 놓고 *"첨두가 줄었다"* 고 말하면
**시계 수정의 효과와 세션 차이가 섞인다.**

그래서 이 도구는 **같은 세션 라벨끼리만** 비교한다. 기본 비교는
`regular`(22:30~05:00 KST) 이고, 시계 수정 전날 밤 대 수정 후 밤이다.

무엇을 재는가 / 못 재는가
-------------------------
`md_peak_1s` 는 텔레메트리 줄에 5분마다 남는 **집계값**이다. 개별 송신 시각은
로그에 없다. 그래서 이 도구가 말할 수 있는 것은 *"수집기가 스스로 보고한 첨두의
분포"* 뿐이고, *"실제로 초당 몇 건을 보냈나"* 가 아니다. 후자는
`tools/replay_send_time.py` 가 오프라인 재계상으로 다룬다.

**`http_429`·`http_429_under_own_limit` 은 수집기 수명 누적값**이라 재시작마다
0 으로 돌아간다. 그래서 분포가 아니라 **구간 증가분**으로 낸다.
"""
from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter
from pathlib import Path

# 텔레메트리 줄에서 뽑는 것들. 값이 없는 줄은 건너뛴다.
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
_SESSION = re.compile(r"session=([a-z]+)")
_FIELDS = ("md_peak_1s", "md_p95_1s", "over_limit_1s",
           "http_429", "http_429_under_own_limit")


def _num(line: str, key: str):
    m = re.search(rf"\b{key}=([0-9.]+)", line)
    if not m:
        return None
    v = m.group(1)
    return float(v) if "." in v else int(v)


def collect(log: Path, day_from: str, time_from: str,
            day_to: str, time_to: str, session: str) -> list[dict]:
    """[from, to) 구간에서 지정 세션의 텔레메트리 표본을 모은다."""
    out = []
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _TS.match(line)
            if not m:
                continue
            d, t = m.group(1), m.group(2)
            if (d, t) < (day_from, time_from) or (d, t) >= (day_to, time_to):
                continue
            s = _SESSION.search(line)
            if not s or s.group(1) != session:
                continue
            if _num(line, "md_peak_1s") is None:
                continue
            row = {"day": d, "time": t}
            for k in _FIELDS:
                row[k] = _num(line, k)
            out.append(row)
    return out


def describe(rows: list[dict], limit: int) -> dict:
    """첨두 분포와 한도 초과 비율. 표본이 없으면 빈 dict."""
    if not rows:
        return {}
    peaks = [r["md_peak_1s"] for r in rows]
    over = [p for p in peaks if p > limit]
    p95s = [r["md_p95_1s"] for r in rows if r["md_p95_1s"] is not None]
    # 누적 카운터는 재시작마다 0 으로 돌아간다 -> 증가분만 센다.
    def delta(key):
        vals = [r[key] for r in rows if r[key] is not None]
        if not vals:
            return None
        total, prev = 0, None
        for v in vals:
            if prev is not None and v >= prev:
                total += v - prev
            prev = v
        return total
    return {
        "n": len(rows),
        "median": statistics.median(peaks),
        "max": max(peaks),
        "over": len(over),
        "over_pct": 100.0 * len(over) / len(peaks),
        "p95_median": statistics.median(p95s) if p95s else None,
        "dist": Counter(peaks),
        "http_429_delta": delta("http_429"),
        "under_own_delta": delta("http_429_under_own_limit"),
        "span": f'{rows[0]["day"]} {rows[0]["time"]} ~ {rows[-1]["day"]} {rows[-1]["time"]}',
    }


def _fmt(label: str, d: dict, limit: int) -> str:
    if not d:
        return f"{label:<22} 표본 없음"
    dist = " ".join(f"{k}:{v}" for k, v in sorted(d["dist"].items()))
    return (f"{label:<22} n={d['n']:>3}  중앙={d['median']:>4}  최대={d['max']:>3}  "
            f">{limit}={d['over']:>3} ({d['over_pct']:>5.1f}%)  "
            f"p95중앙={d['p95_median']}\n"
            f"{'':<22} 분포 {dist}\n"
            f"{'':<22} 429 증가 {d['http_429_delta']} · 자기한도내 429 증가 {d['under_own_delta']}\n"
            f"{'':<22} 구간 {d['span']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="data/collector.log")
    ap.add_argument("--session", default="regular",
                    help="비교할 세션 라벨 (기본 regular)")
    ap.add_argument("--limit", type=int, default=10,
                    help="MARKET_DATA 공시 한도 (기본 10)")
    ap.add_argument("--before", nargs=4, metavar=("D1", "T1", "D2", "T2"),
                    required=True, help="수정 전 구간 [D1 T1, D2 T2)")
    ap.add_argument("--after", nargs=4, metavar=("D1", "T1", "D2", "T2"),
                    required=True, help="수정 후 구간 [D1 T1, D2 T2)")
    # 라벨을 인자로 뺀다: 검증용으로 수정 전 구간 둘을 비교할 때
    # "수정 후" 라고 찍히면 그 출력이 그대로 인용돼 거짓이 된다.
    ap.add_argument("--labels", nargs=2, metavar=("A", "B"),
                    default=["수정 전 (벽시계)", "수정 후 (단조시계)"])
    a = ap.parse_args()

    log = Path(a.log)
    before = describe(collect(log, *a.before, a.session), a.limit)
    after = describe(collect(log, *a.after, a.session), a.limit)

    print(f"세션={a.session}  한도={a.limit}  (같은 세션끼리만 비교한다 — 부하가 다르면 첨두도 다르다)")
    print("-" * 108)
    print(_fmt(a.labels[0], before, a.limit))
    print()
    print(_fmt(a.labels[1], after, a.limit))
    print("-" * 108)
    if before and after:
        print(f"한도 초과 비율 {before['over_pct']:.1f}% -> {after['over_pct']:.1f}%  "
              f"(중앙 {before['median']} -> {after['median']})")
        if after["over"] == 0 and before["over"] > 0:
            print("수정 후 구간에서 한도 초과 표본이 0 이다.")
        elif after["over"] > 0:
            print("★ 수정 후에도 초과가 남아 있다 — 시계 말고 다른 원인이 있다는 뜻이다.")
    else:
        print("한쪽 구간에 표본이 없어 비교하지 않는다.")


if __name__ == "__main__":
    main()

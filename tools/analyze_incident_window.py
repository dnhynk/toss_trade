"""사고 구간 법의학 — 우리 계측의 '한도 초과' 판정과 서버의 429 를 줄 단위로 대조한다.

`docs/32` §0 의 핵심 주장(계측기와 서버가 양방향으로 어긋난다)을 사고 구간에서 세는 도구.
쓰기 없음 · 라이브 호출 없음 · 로그만 읽는다.

    python tools/analyze_incident_window.py <collector.log> <YYYY-MM-DD> [시작HH:MM] [끝HH:MM]

날짜를 반드시 받는다 — 로그가 여러 날에 걸쳐 있어 시각만으로 거르면 **다른 날의 사건이
집계에 섞인다** (실제로 07-31·08-03 의 축소 4건이 섞여 강등 누계가 16개 부풀었다).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TS = r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ "
RE_OVER = re.compile(TS + r"ERROR\s+budget: (?P<group>\S+) 1초에 (?P<n>\d+)회 — 공시 한도 (?P<lim>\d+) 초과")
RE_SHRINK = re.compile(TS + r"WARNING budget shrink (?P<what>\{[^}]*\}) → caps=\{tier2:(?P<t2>\d+), tier3:(?P<t3>\d+)\} demoted=(?P<dem>\d+)")
RE_START = re.compile(TS + r"INFO\s+collector start")
RE_TELEM = re.compile(TS + r"INFO\s+telemetry .*?\bhttp_429=(?P<h429>\d+)\b.*?\bover_limit_1s=(?P<ov>\d+)\b")
RE_TELEM2 = re.compile(TS + r"INFO\s+telemetry .*?\bover_limit_1s=(?P<ov>\d+)\b.*?\bhttp_429=(?P<h429>\d+)\b")
RE_429 = re.compile(TS + r"WARNING HTTP-429-DETAIL")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    day = sys.argv[2]
    lo = sys.argv[3] if len(sys.argv) > 3 else "00:00"
    hi = sys.argv[4] if len(sys.argv) > 4 else "23:59"

    events = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        hhmm = raw[11:16]
        if not (raw[:10] == day and lo <= hhmm <= hi):
            continue
        for kind, rx in (("OVER", RE_OVER), ("SHRINK", RE_SHRINK), ("START", RE_START),
                         ("429", RE_429), ("TELEM", RE_TELEM), ("TELEM", RE_TELEM2)):
            m = rx.match(raw)
            if m:
                events.append((m.group("ts"), kind, m.groupdict()))
                break

    print(f"구간 {lo}~{hi}  이벤트 {len(events)}건\n")
    print(f"{'시각':<21}{'종류':<8}내용")
    print("-" * 100)
    n_over = n_shrink = n_429 = demoted_total = 0
    h429_seen: list[tuple[str, str]] = []
    for ts, kind, d in events:
        if kind == "OVER":
            n_over += 1
            body = f"우리 계측: {d['group']} 1초에 {d['n']}회 (한도 {d['lim']}) — 초과 판정"
        elif kind == "SHRINK":
            n_shrink += 1
            demoted_total += int(d["dem"])
            body = f"정원 축소 {d['what']} → tier2={d['t2']} tier3={d['t3']} 강등={d['dem']}"
        elif kind == "START":
            body = "*** 수집기 재기동 (카운터 리셋) ***"
        elif kind == "429":
            n_429 += 1
            body = "서버 429 관측"
        else:
            h429_seen.append((ts, d["h429"]))
            body = f"telemetry: http_429={d['h429']} over_limit_1s={d['ov']}"
        print(f"{ts:<21}{kind:<8}{body}")

    print("\n" + "=" * 100)
    print(f"우리 계측의 '한도 초과' 판정 : {n_over}건")
    print(f"서버의 429                   : {n_429}건")
    print(f"정원 축소                    : {n_shrink}건, 강등 종목 누계 {demoted_total}개")
    if h429_seen:
        vals = [v for _t, v in h429_seen]
        # 재기동은 카운터를 0 으로 리셋한다 — 그 하강은 429 가 아니다.
        # 진짜 429 는 **같은 프로세스 안에서 값이 오르는 것**뿐이다.
        rises = sum(1 for a, b in zip(vals, vals[1:]) if int(b) > int(a))
        print(f"telemetry http_429 추이      : {' → '.join(vals)}")
        print(f"  같은 프로세스 안의 상승(=진짜 429): {rises}회"
              f"   {'(하강은 재기동 리셋이다)' if any(int(b) < int(a) for a, b in zip(vals, vals[1:])) else ''}")
    print("=" * 100)
    if n_over and not n_429:
        print("판정: 이 구간에서 서버는 429 를 한 건도 주지 않았다.")
        print(f"      그런데 우리 계측은 {n_over}회 '한도 초과' 를 선언했고 그 결과 "
              f"{demoted_total}개 종목이 강등됐다.")
        print("      → 축소를 유발한 판정에 서버 측 확인이 **하나도 없다**.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

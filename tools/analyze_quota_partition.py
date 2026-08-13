"""한도 헤더가 **무엇의 한도인가** — `HTTP-429-DETAIL` 원문으로 구획을 규명한다.

배경. docs/06 §9-6 은 "429 의 원인이 우리 밖에 있다" 를 남겼고 docs/52 §12 는 그것을
429 없이 보는 관측(`foreign`)을 만들었다. 그러나 `foreign` 은 **다른 발신자**와
**그룹 밖 한도**를 못 가른다. 가르려면 먼저 서버가 말하는 한도가 무엇의 한도인지
알아야 한다 — 같은 경로가 5·15·20 을 함께 보고하고 있다.

이 도구는 **라이브 호출을 하지 않는다.** 이미 쌓인 로그만 읽는다 (읽기 전용).

읽는 것:
  * `x-ratelimit-limit` 의 **시간 구조** — 값이 시각의 함수인가(서버측 변경/세션),
    경로의 함수인가, 그룹의 함수인가.
  * `x-ratelimit-remaining` 과 `limit` 의 관계 — 429 인데 잔량이 남아 있으면 그 헤더는
    거절한 바구니가 아니다.
  * `x-ratelimit-reset` · `date` — 창의 길이와 이름.

사용: python -m tools.analyze_quota_partition [로그경로]
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_LOG = Path(r"C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\data\collector.log")

# 이 줄의 형식은 세 번 바뀌었고 로그가 회전되지 않아 **셋이 섞여 있다.** 고정 정규식으로
# 한 형식만 잡으면 표본이 조용히 절반이 된다(처음 그렇게 짰다가 335줄을 버렸다). 그래서
# 꼬리의 `headers={...}` 만 떼어내고 앞부분은 `key=value` 로 훑는다.
HEAD = re.compile(r"^(?P<ts>[\d-]+ [\d:,]+) WARNING HTTP-429-DETAIL (?P<body>.*)$")
TAIL = re.compile(r"\s*headers=(?P<hdrs>\{.*\})\s*$")
PEAKS = re.compile(r"peak1s=(?P<peaks>\{[^}]*\})")
FIELD = re.compile(r"\b([a-z_0-9]+)=([^ ]+)")

# 헤더의 출처. 이 구분이 분석의 전부다 —
#   "429"   : `client.last_429` 에서 온 원문. 429 응답 그 자리에서 찍혀 덮어쓰이지 않는다.
#   "last"  : 옛 줄. `client.last_headers` 라 **뒤따르는 200 이 덮어쓴 것**일 수 있다
#             (docs/45 §5 에서 실제로 그렇게 오독했다). status=200 이면 확실히 오염이다.
TRUST_429, TRUST_LAST = "429", "last"

TELEMETRY = re.compile(r"^(?P<ts>[\d-]+ [\d:,]+) INFO\s+telemetry session=(?P<session>\S+) ")


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def parse(log: Path) -> tuple[list[dict], list[tuple[str, str]]]:
    rows: list[dict] = []
    sessions: list[tuple[str, str]] = []
    unparsed: list[str] = []
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "telemetry session=" in raw:
            m = TELEMETRY.match(raw)
            if m:
                sessions.append((m.group("ts"), m.group("session")))
            continue
        if "HTTP-429-DETAIL" not in raw:
            continue
        m = HEAD.match(raw.strip())
        t = TAIL.search(m.group("body")) if m else None
        if not m or not t:
            unparsed.append(raw.strip())
            continue
        body = m.group("body")[:t.start()]
        try:
            hdrs = {k.lower(): v for k, v in ast.literal_eval(t.group("hdrs")).items()}
        except (ValueError, SyntaxError):
            unparsed.append(raw.strip())
            continue
        pk = PEAKS.search(body)
        f = dict(FIELD.findall(PEAKS.sub("", body)))
        d = {
            "ts": m.group("ts"),
            "group": f.get("group", "?"),
            "caller": f.get("caller", "?"),
            "status": _int(f.get("status")),
            "path": f.get("path", "?"),
            "own_in_server_s": _int(f.get("own_in_server_s")),
            "limit_hdr_field": _int(f.get("limit_hdr")),
            "under_own": f.get("under_own_limit"),
            "peaks": ast.literal_eval(pk.group("peaks")) if pk else None,
            "hdrs": hdrs,
            "limit": _int(hdrs.get("x-ratelimit-limit")),
            "remaining": _int(hdrs.get("x-ratelimit-remaining")),
            "reset": _int(hdrs.get("x-ratelimit-reset")),
            "date": hdrs.get("date", ""),
        }
        # `limit_hdr=` 필드가 있으면 그 줄의 헤더는 429 원문이다 (같은 `last_429` 에서 왔다).
        d["trust"] = TRUST_429 if "limit_hdr" in f else TRUST_LAST
        rows.append(d)
    if unparsed:
        print(f"[경고] 파싱 실패 {len(unparsed)}줄 — 표본이 줄어든 것을 숨기지 않는다")
        for raw in unparsed[:3]:
            print("  UNPARSED:", raw[:200])
    return rows, sessions


def session_at(sessions: list[tuple[str, str]], ts: str) -> str:
    """이 429 직전 telemetry 줄이 말한 세션 (없으면 '?')."""
    best = "?"
    for sts, s in sessions:
        if sts <= ts:
            best = s
        else:
            break
    return best


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))


def main() -> None:
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    rows, sessions = parse(log)
    trusted = [r for r in rows if r["trust"] == TRUST_429]
    legacy = [r for r in rows if r["trust"] == TRUST_LAST]
    print(f"로그: {log}")
    print(f"HTTP-429-DETAIL {len(rows)}줄 (429 원문 {len(trusted)} / 옛 last_headers {len(legacy)}), "
          f"telemetry {len(sessions)}줄")
    print("아래 분석은 **429 원문 줄만** 쓴다. 옛 줄은 마지막 섹션에서 따로 본다.")

    section("1. limit 헤더 × 경로 × 그룹(owner) — 429 원문만")
    tab: Counter = Counter((r["limit"], r["path"], r["group"]) for r in trusted)
    print(f"{'limit':>6} {'path':<30}{'group(owner)':<22}{'건수':>6}")
    for (lim, path, grp), n in sorted(tab.items(), key=lambda kv: -kv[1]):
        print(f"{str(lim):>6} {path:<30}{grp:<22}{n:>6}")
    print("→ 한 경로가 여러 limit 을 보고하면 그 헤더는 **경로의 한도가 아니다**.")

    section("2. limit 의 시간 구조 — 연속 구간")
    runs: list[list] = []
    for r in trusted:
        if runs and runs[-1][0] == r["limit"]:
            runs[-1][2] = r["ts"]
            runs[-1][3] += 1
        else:
            runs.append([r["limit"], r["ts"], r["ts"], 1])
    print(f"연속 구간 {len(runs)}개 / {len(trusted)}줄 "
          f"(구간이 줄 수에 가까우면 값이 **뒤섞여** 있다 = 시각의 함수가 아니다)")
    for lim, first, last, n in runs[:40]:
        print(f"{str(lim):>6} {first:<24}{last:<24}{n:>6}")
    if len(runs) > 40:
        print(f"  ... {len(runs) - 40}개 더")

    section("3. 같은 초 / 같은 분 안에서 limit 이 갈리는가")
    for width, name in ((19, "초"), (16, "분")):
        by: dict[str, set] = defaultdict(set)
        for r in trusted:
            by[r["ts"][:width]].add(r["limit"])
        mixed = {k: v for k, v in by.items() if len(v) > 1}
        print(f"같은 {name}에 서로 다른 limit: {len(mixed)}/{len(by)}{name}")
        for k, v in sorted(mixed.items())[:12]:
            print(f"  {k}  limits={sorted(x for x in v if x is not None)}")

    section("4. limit × 세션")
    ses: Counter = Counter((r["limit"], session_at(sessions, r["ts"])) for r in trusted)
    for (lim, s), n in sorted(ses.items(), key=lambda kv: -kv[1]):
        print(f"  limit={str(lim):<5} session={s:<10} {n:>5}건")

    section("5. (limit, remaining, reset) 결합분포 — 429 인데 잔량이 남았나")
    rem: Counter = Counter((r["limit"], r["remaining"], r["reset"]) for r in trusted)
    print(f"{'limit':>6}{'remain':>8}{'reset':>7}{'건수':>7}   consumed=limit-remaining")
    for (lim, rm, rs), n in sorted(rem.items(), key=lambda kv: -kv[1]):
        cons = "?" if lim is None or rm is None else str(lim - rm)
        print(f"{str(lim):>6}{str(rm):>8}{str(rs):>7}{n:>7}   consumed={cons}")

    section("6. limit 값별 우리 송신 첨두 / own_in_server_s")
    for lim in sorted({r["limit"] for r in trusted if r["limit"] is not None}):
        sub = [r for r in trusted if r["limit"] == lim]
        pk = [r for r in sub if r["peaks"]]
        own = [r["own_in_server_s"] for r in sub if r["own_in_server_s"] is not None]
        def stat(key):
            v = [r["peaks"].get(key, 0) for r in pk]
            return f"평균{sum(v)/len(v):.2f} 최대{max(v)}" if v else "-"
        print(f"  limit={lim:<3} n={len(sub):<4} MD({stat('MARKET_DATA')}) "
              f"CHART({stat('MARKET_DATA_CHART')}) RANK({stat('RANKING')}) "
              f"own_in_server_s={sorted(set(own))}")

    section("7. own_in_server_s 분포")
    for k, n in sorted(Counter(r["own_in_server_s"] for r in trusted).items(),
                       key=lambda kv: (kv[0] is None, kv[0])):
        print(f"  own_in_server_s={str(k):<5} {n:>5}건")

    section("8. group(owner) × caller — caller 가 무엇인지")
    for (g, c), n in sorted(Counter((r["group"], r["caller"]) for r in trusted).items(),
                            key=lambda kv: -kv[1]):
        print(f"  group={g:<20} caller={c:<20} {n:>5}건  {'≠' if g != c else '='}")

    section("9. 옛 줄(last_headers) — status 별로 나눠서만 본다")
    for st in sorted({r["status"] for r in legacy}, key=lambda x: (x is None, x)):
        sub = [r for r in legacy if r["status"] == st]
        lims = Counter(r["limit"] for r in sub)
        note = ("오염 — 이 헤더는 뒤따른 200 의 것이다"
                if st == 200 else "429 응답의 헤더로 볼 수 있다")
        print(f"  status={st} n={len(sub)} limits={dict(lims)}  ({note})")
        grp = Counter((r["group"], r["limit"]) for r in sub)
        for (g, l), n in sorted(grp.items(), key=lambda kv: -kv[1])[:8]:
            print(f"      group={g:<20} limit={str(l):<5} {n:>5}건")

    section("10. 429 의 시간 구조 — 어떤 창이 거절하는가")
    from datetime import datetime
    def t(r):
        return datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S,%f")
    for lim in sorted({r["limit"] for r in trusted if r["limit"] is not None}):
        sub = [r for r in trusted if r["limit"] == lim]
        if len(sub) < 3:
            continue
        gaps = [(t(b) - t(a)).total_seconds() for a, b in zip(sub, sub[1:])]
        gaps = [g for g in gaps if g >= 0]
        per_min = Counter(r["ts"][:16] for r in sub)
        per_sec = Counter(r["ts"][:19] for r in sub)
        print(f"  limit={lim}  n={len(sub)}")
        print(f"    같은 분에 2건 이상: {sum(1 for v in per_min.values() if v > 1)}/{len(per_min)}분 "
              f"(최대 {max(per_min.values())}건/분)")
        print(f"    같은 초에 2건 이상: {sum(1 for v in per_sec.values() if v > 1)}/{len(per_sec)}초")
        gaps_s = sorted(gaps)
        q = lambda f: gaps_s[min(len(gaps_s) - 1, int(len(gaps_s) * f))]
        print(f"    간격(초) 중위 {q(0.5):.1f} / 10%p {q(0.1):.1f} / 90%p {q(0.9):.1f} / 최소 {gaps_s[0]:.2f}")
        print(f"    간격 <2초 비율 {sum(1 for g in gaps if g < 2)/len(gaps):.1%}  "
              f"<60초 비율 {sum(1 for g in gaps if g < 60)/len(gaps):.1%}")
        soms = Counter(int(r["ts"][17:19]) for r in sub)
        top = sorted(soms.items(), key=lambda kv: -kv[1])[:6]
        print(f"    분내 초 상위: {top} (한 초에 몰리면 루프 동시 발화)")

    section("11. 429 의 date 초 vs 로그 시각 — 같은 초인가")
    from email.utils import parsedate_to_datetime
    same = diff = bad = 0
    deltas: Counter = Counter()
    for r in trusted:
        try:
            d = parsedate_to_datetime(r["date"])
        except (TypeError, ValueError):
            bad += 1
            continue
        # 로그 시각은 KST 로컬, date 는 GMT. 초만 비교한다 (분·초 정렬은 같다).
        log_s = int(r["ts"][17:19])
        dd = (d.second - log_s) % 60
        deltas[dd if dd <= 30 else dd - 60] += 1
        same += 1 if dd == 0 else 0
        diff += 1 if dd != 0 else 0
    print(f"  date 초 == 로그 초: {same}, 다름: {diff}, 파싱실패: {bad}")
    print(f"  (date초 - 로그초) 분포: {dict(sorted(deltas.items()))}")
    print("  → 0 이 아닌 값은 서버가 응답을 만든 초와 우리가 로그를 찍은 초의 차이다.")

    section("12. ★ 서버가 센 소진량 vs 우리가 센 우리 몫 — 같은 서버 초에서")
    #
    # 이것이 (A)/(B) 를 가르는 자리다. 두 값은 **같은 429 응답 하나**에서 나온다:
    #   consumed = limit - remaining  (서버가 그 바구니에서 센 총량)
    #   own      = own_in_server_s    (client 가 그 (그룹, date 초) 에서 센 우리 송신)
    # 둘이 같다면 그 바구니에는 우리밖에 없다. consumed 가 크면 우리가 안 보낸 소비가 있다.
    #
    # 주의: 429 응답의 잔량은 docs/06 §9-3 이 "다음 창일 수 있다" 고 유보한 값이다.
    # 그래서 아래 표는 **일치 자체가 그 유보를 검증**한다 — 다음 창이면 consumed 는 늘 0~1 일
    # 텐데 own 과 같이 움직이면 같은 창을 보고 있다는 뜻이다.
    joint: Counter = Counter()
    for r in trusted:
        if r["limit"] is None or r["remaining"] is None or r["own_in_server_s"] is None:
            continue
        joint[(r["limit"] - r["remaining"], r["own_in_server_s"])] += 1
    tot = sum(joint.values())
    print(f"  표본 {tot}건")
    print(f"  {'consumed':>9}{'own':>6}{'건수':>7}{'비율':>8}   판정")
    for (c, o), n in sorted(joint.items()):
        verdict = ("일치 — 이 바구니엔 우리뿐" if c == o else
                   f"우리가 안 보낸 소비 {c - o}건" if c > o else
                   f"우리 송신이 서버 카운터보다 {o - c}건 많다 (창 어긋남)")
        print(f"  {c:>9}{o:>6}{n:>7}{n/tot:>7.1%}   {verdict}")
    eq = sum(n for (c, o), n in joint.items() if c == o)
    gt = sum(n for (c, o), n in joint.items() if c > o)
    lt = sum(n for (c, o), n in joint.items() if c < o)
    print(f"  consumed == own : {eq}/{tot} ({eq/tot:.1%})")
    print(f"  consumed >  own : {gt}/{tot} ({gt/tot:.1%})  ← 남의 소비 방향")
    print(f"  consumed <  own : {lt}/{tot} ({lt/tot:.1%})  ← 창 어긋남 방향")

    section("13. ★ 초과 소비가 own 과 독립인가 — (A) 를 가르는 검정")
    #
    # 두 모델의 예측이 다르다:
    #   (A) 다른 발신자 — 그의 송신은 우리 응답 도착 순서와 무관하다. 따라서 초과가
    #       own=1·2·3 어디서나 **비슷한 비율**로 보여야 한다.
    #   (C) 도착 순서 artifact — 429 는 200 보다 빨리 돌아온다(거절은 싸다). 그래서 우리
    #       형제 요청들의 응답이 아직 안 왔을 때 `own` 이 과소집계되고, 그 상태는
    #       **own 이 작을 때만** 성립한다. 초과는 own=1 에 몰려야 한다.
    by_own: dict[int, list[int]] = defaultdict(list)
    zero = 0
    for r in trusted:
        if r["limit"] is None or r["remaining"] is None or r["own_in_server_s"] is None:
            continue
        c = r["limit"] - r["remaining"]
        zero += 1 if c == 0 else 0
        by_own[r["own_in_server_s"]].append(c - r["own_in_server_s"])
    print(f"  consumed==0 인 줄: {zero}건")
    print("  → 0 건이면 서버는 **거절한 요청도 카운터에 센다** (우리 429 도 소진량에 들어 있다).")
    print("     그래야 '우리 응답이 그 429 하나뿐인 창' 에서 consumed 가 1 이 된다.")
    print()
    print(f"  {'own':>5}{'표본':>7}{'초과>0':>8}{'초과율':>9}{'초과 최대':>9}")
    for own in sorted(by_own):
        d = by_own[own]
        pos = sum(1 for x in d if x > 0)
        print(f"  {own:>5}{len(d):>7}{pos:>8}{pos/len(d):>8.1%}{max(d):>9}")
    hi = [x for own, d in by_own.items() if own >= 2 for x in d]
    lo = by_own.get(1, [])
    if lo and hi:
        rate = sum(1 for x in lo if x > 0) / len(lo)
        pos_hi = sum(1 for x in hi if x > 0)
        print(f"\n  own=1 초과율 {rate:.1%} / own>=2 초과 {pos_hi}건 / {len(hi)}건")
        print(f"  own>=2 에서 같은 비율이라면 기대 {rate*len(hi):.1f}건, 관측 {pos_hi}건.")
        print(f"  (A) 가정하의 확률 P(0건) = (1-{rate:.3f})^{len(hi)} = {(1-rate)**len(hi):.2e}")
        print("  → own 과 독립이 아니다. 초과는 남의 소비가 아니라 **우리 응답 도착 순서**다.")


if __name__ == "__main__":
    main()

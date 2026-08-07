"""`ranking_cadence` 프로브 JSON → `docs/35` 의 표. 소유: W1.

손으로 옮기다 숫자를 틀리지 않기 위한 것이고, **세션 재측정 때 같은 표를 뽑기 위한 것**이다
(프리마켓과 정규장을 다른 방식으로 요약하면 세션 비교가 아니라 요약 방식 비교가 된다).

    python tools/render_cadence.py data/cadence_open.json

읽기 전용 — API 도 DB 도 건드리지 않는다.
"""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))["ranking_cadence"]


def f(x):
    return "—" if x is None else x


def stat(s):
    if not s:
        return "—"
    return f"n={s['n']} min={s['min']} p50={s['p50']} max={s['max']} mean={s['mean']}"


print("## meta")
print("measured_at_kst =", d.get("measured_at_kst"), " et =", d.get("measured_at_et"))
print("aborted =", d.get("aborted"), " calls_used =", d.get("calls_used"),
      " cap =", d.get("budget", {}).get("call_cap"))
print("collector 429 during probe =", d.get("collector_429_total_during_probe"))
print("baseline:", json.dumps(d.get("collector_baseline_429"), ensure_ascii=False))
print("stop thresholds:", json.dumps(d.get("stop_thresholds"), ensure_ascii=False))
for k in ("telemetry_at_start", "telemetry_at_end"):
    print(k, json.dumps(d.get(k), ensure_ascii=False))

print("\n## oneshot")
for k, v in (d.get("oneshot") or {}).items():
    print(f"- {k}: http={v.get('http')} rows={v.get('rows')} "
          f"rankedAt={v.get('rankedAt')} err={str(v.get('error'))[:110]}")

for sec in ("volume_realtime", "top_gainers_1d"):
    s = d.get(sec)
    if not s or "arms" not in s:
        print(f"\n## {sec}: {s}")
        continue
    print(f"\n## {sec}  params={json.dumps(s['params'], ensure_ascii=False)}")
    print("\n| 팔 | 실제 간격 p50 | 폴 | rankedAt 변화 | 서버 스탬프 델타 | "
          "order 변화 | order 관측간격 p50 | volume 변화 | volume 관측간격 p50 | "
          "all 변화 | all 관측간격 p50 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for label, a in s["arms"].items():
        if "polls" not in a:
            print(f"| {label} | SKIP {a.get('skipped')} |")
            continue
        ra, od, vo, al = a["rankedAt"], a["order"], a["volume"], a["all"]
        print(f"| {label} | {a['actual_gap_s']['p50']} | {a['polls']} | {ra['changes']} | "
              f"{stat(ra.get('server_stamp_delta_s'))} | {od['changes']} | "
              f"{(od['observed_interval_s'] or {}).get('p50', '—')} | {vo['changes']} | "
              f"{(vo['observed_interval_s'] or {}).get('p50', '—')} | {al['changes']} | "
              f"{(al['observed_interval_s'] or {}).get('p50', '—')} |")
    a1 = s["arms"].get("1s", {})
    print("\n1s 팔 부가:")
    print("  stamp_lag_s:", stat(a1.get("rankedAt", {}).get("stamp_lag_s")))
    print("  server_stamp_delta_hist:",
          json.dumps(a1.get("rankedAt", {}).get("server_stamp_delta_hist"), ensure_ascii=False))
    print("  stamp_vs_content:", json.dumps(a1.get("stamp_vs_content"), ensure_ascii=False))
    print("  errors:", a1.get("errors"), " collector_429_during:", a1.get("collector_429_during"))
    print("  last/rate 변화:", a1.get("last", {}).get("changes"),
          "/", a1.get("rate", {}).get("changes"))

    dec = a1.get("decimated") or {}
    if dec:
        print("\n### 솎기 (같은 1s 응답열을 굵은 자로 다시 읽기)")
        print("| stride(=유효 주기) | 폴 | order 변화 | order 관측간격 p50 | "
              "volume 관측간격 p50 | all 변화 | all 관측간격 p50 | rankedAt 변화 | 서버 스탬프 델타 p50 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for k, v in dec.items():
            if "order" not in v:
                print(f"| {k} | {v.get('note')} |")
                continue
            g = v["actual_gap_s"]["p50"]
            print(f"| {k} (~{g}s) | {v['polls']} | {v['order']['changes']} | "
                  f"{(v['order']['observed_interval_s'] or {}).get('p50', '—')} | "
                  f"{(v['volume']['observed_interval_s'] or {}).get('p50', '—')} | "
                  f"{v['all']['changes']} | "
                  f"{(v['all']['observed_interval_s'] or {}).get('p50', '—')} | "
                  f"{v['rankedAt']['changes']} | "
                  f"{(v['rankedAt'].get('server_stamp_delta_s') or {}).get('p50', '—')} |")

print("\n## universe_overlap")
for k, v in (d.get("universe_overlap") or {}).items():
    if not isinstance(v, dict):
        print(f"- {k}: {v}")
        continue
    v = dict(v)
    v.pop("symbols", None)
    print(f"- {k}: {json.dumps(v, ensure_ascii=False)}")

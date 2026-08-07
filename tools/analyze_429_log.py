"""429 로그 법의학 — HTTP-429-DETAIL 파싱 + 결합 첨두 상·하계 산출."""
import ast
import re
from pathlib import Path

LOG = Path(r"C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\data\collector.log")
# 2026-08-08 (W1): 이 줄의 출처가 `client.last_headers` → `client.last_429` 로 바뀌면서
# `since=` 와 서버측 진단 필드(`under_own_limit` 등)가 붙었다 (docs/45 §5). 옛 줄과 새 줄을
# **둘 다** 읽어야 한다 — 로그가 회전되지 않는 단일 파일이라 두 형식이 섞여 있다.
LINE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) WARNING HTTP-429-DETAIL group=(?P<group>\S+) "
    r"caller=(?P<caller>\S+) attributed=(?P<attr>\S+)(?: since=(?P<since>\d+))? "
    r"status=(?P<status>\S+)"
    r"(?: peak1s=(?P<peaks>\{[^}]*\}) own_within_limit=(?P<own>\S+) "
    r"md_plus_chart=(?P<fam>\d+))?"
    r"(?P<extra>(?: [a-z_]+=\S+)*) headers=(?P<hdrs>\{.*\})\s*$")

LIMITS = {"MARKET_DATA": 10, "MARKET_DATA_CHART": 5, "RANKING": 5}

rows = []
for raw in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
    if "HTTP-429-DETAIL" not in raw:
        continue
    m = LINE.match(raw.strip())
    if not m:
        print("UNPARSED:", raw[:160])
        continue
    d = m.groupdict()
    d["peaks"] = ast.literal_eval(d["peaks"]) if d["peaks"] else None
    d["hdrs"] = ast.literal_eval(d["hdrs"])
    rows.append(d)

print(f"총 {len(rows)}줄\n")
hdr = (f"{'시각':<13}{'caller':<18}{'MD':>3}{'CH':>3}{'RK':>3}"
       f"{'  hdr-limit':>11}{'  hdr-rem':>9}{'  joint하계':>10}{'  joint상계':>10}"
       f"{'  +RK상계':>9}")
print(hdr)
print("-" * len(hdr))
for r in rows:
    t = r["ts"][11:19]
    p = r["peaks"] or {}
    md, ch, rk = p.get("MARKET_DATA", 0), p.get("MARKET_DATA_CHART", 0), p.get("RANKING", 0)
    lo, hi = max(md, ch), md + ch
    print(f"{t:<13}{r['caller']:<18}{md:>3}{ch:>3}{rk:>3}"
          f"{r['hdrs'].get('x-ratelimit-limit', '?'):>11}"
          f"{r['hdrs'].get('x-ratelimit-remaining', '?'):>9}"
          f"{lo:>10}{hi:>10}{hi + rk:>9}")

print("\n--- 헤더 status/limit 정합성 ---")
for r in rows:
    lim = r["hdrs"].get("x-ratelimit-limit")
    exp = LIMITS.get(r["caller"])
    print(f"{r['ts'][11:19]}  status={r['status']:<5} caller={r['caller']:<18}"
          f"hdr-limit={lim:<4} caller그룹공시={exp}  일치={lim == str(exp)}")

with_peaks = [r for r in rows if r["peaks"]]
print(f"\n--- 상계가 10 미만인 줄 (공유 한도 10 가설과 모순) ---")
for r in with_peaks:
    p = r["peaks"]
    hi = p["MARKET_DATA"] + p["MARKET_DATA_CHART"]
    if hi < 10:
        print(f"  {r['ts'][11:19]}  MD+CH 상계={hi}  (+RANKING={hi + p['RANKING']})")
n_lt = sum(1 for r in with_peaks
           if r["peaks"]["MARKET_DATA"] + r["peaks"]["MARKET_DATA_CHART"] < 10)
n_lt3 = sum(1 for r in with_peaks
            if sum(r["peaks"].values()) < 10)
print(f"\nMD+CHART 상계 < 10 : {n_lt}/{len(with_peaks)}")
print(f"MD+CHART+RANKING 상계 < 10 : {n_lt3}/{len(with_peaks)}")

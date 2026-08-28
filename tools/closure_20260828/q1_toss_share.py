import sqlite3, time, datetime as dt, statistics as st
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180)
con.execute("PRAGMA query_only=ON")
cur = con.cursor()
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")   # exploration-B era only; confirmation arm (>=08-26) untouched
KST = 9*3600*1000
def session(snap):
    m = int(((snap + KST) % 86400000) // 60000)
    if 540 <= m < 1020: return "day"
    if 1020 <= m < 1350: return "pre"
    if m >= 1350 or m < 300: return "regular"
    if 300 <= m < 530: return "after"
    return "closed"
def band(p):
    return "$0-2" if p < 2 else "$2-5" if p < 5 else "$5-20" if p < 20 else "$20+"
t=time.time()
# 1) do TOSS and MARKET lists share snap_ms?
r = cur.execute("""SELECT COUNT(*) FROM (SELECT DISTINCT snap_ms FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND snap_ms>=? AND snap_ms<?) t
  JOIN (SELECT DISTINCT snap_ms FROM rankings_snap WHERE ranking_type='MARKET_TRADING_VOLUME' AND snap_ms>=? AND snap_ms<?) m ON t.snap_ms=m.snap_ms""",(A,B,A,B)).fetchone()
print("shared snap_ms count (TOSS∩MARKET):", r, f"[{time.time()-t:.0f}s]")
# 2) join same symbol, same snap_ms (fallback: nearest MARKET snap within 15s if not shared)
t=time.time()
rows = cur.execute("""
SELECT t.snap_ms, t.symbol, t.rank, m.rank, t.vol_qu, m.vol_qu, t.last_u
FROM rankings_snap t JOIN rankings_snap m
  ON m.symbol=t.symbol AND m.ranking_type='MARKET_TRADING_VOLUME' AND m.duration='realtime'
  AND m.snap_ms BETWEEN t.snap_ms-8000 AND t.snap_ms+8000
WHERE t.ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND t.duration='realtime'
  AND t.snap_ms>=? AND t.snap_ms<? AND m.vol_qu>0 AND t.vol_qu>0
  AND t.amount_u <> 9223372036854775807 AND m.amount_u <> 9223372036854775807
""",(A,B)).fetchall()
print(f"joined rows: {len(rows)} [{time.time()-t:.0f}s]")
from collections import defaultdict
# dedupe: one MARKET match per (t.snap_ms, symbol): keep first
seen=set(); R=[]
for r in rows:
    k=(r[0],r[1])
    if k in seen: continue
    seen.add(k); R.append(r)
print("deduped:", len(R))
def pct(xs, q): 
    xs=sorted(xs); 
    return xs[min(len(xs)-1, int(q*len(xs)))] if xs else None
grp=defaultdict(list); grp_sym=defaultdict(set)
for snap,sym,tr,mr,tv,mv,lp in R:
    sh = tv/mv
    key=(session(snap), band(lp/1e6))
    grp[key].append(sh); grp_sym[key].add(sym)
print("\nToss share = vol_qu(TOSS)/vol_qu(MARKET), same symbol, |dt|<=8s. Exploration-B era 08-18..08-25 (all sessions).")
print(f"{'session':8} {'band':6} {'n':>7} {'syms':>5} {'p10':>6} {'p25':>6} {'p50':>6} {'p75':>6} {'p90':>6} {'>0.5':>6}")
for key in sorted(grp):
    xs=grp[key]
    print(f"{key[0]:8} {key[1]:6} {len(xs):7} {len(grp_sym[key]):5} {pct(xs,.1):6.3f} {pct(xs,.25):6.3f} {pct(xs,.5):6.3f} {pct(xs,.75):6.3f} {pct(xs,.9):6.3f} {sum(x>0.5 for x in xs)/len(xs):6.2f}")
# 3) calibration: mega caps
print("\nCalibration (regular session), median share for well-known large caps:")
big=defaultdict(list)
for snap,sym,tr,mr,tv,mv,lp in R:
    if session(snap)=="regular" and sym in ("AAPL","NVDA","TSLA","AMZN","MSFT","AMD","PLTR","SOXL","TQQQ","META","GOOGL","INTC","OPEN","SOFI","RIVN","NIO","LCID","BBAI","MSTR","COIN"):
        big[sym].append(tv/mv)
for s in sorted(big, key=lambda s: st.median(big[s])):
    print(f"  {s:6} n={len(big[s]):5} p50={st.median(big[s]):.4f}  p90={pct(big[s],.9):.4f}")
# 4) share >1 count
allsh=[tv/mv for _,_,_,_,tv,mv,_ in R]
print(f"\nshare>1.0: {sum(x>1 for x in allsh)/len(allsh):.3%}   share>2.0: {sum(x>2 for x in allsh)/len(allsh):.3%}")

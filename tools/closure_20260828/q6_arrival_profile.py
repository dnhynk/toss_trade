# Descriptive forward profile of TRUE ARRIVALS (origin>=31 / outside top100) vs boundary flicker, ruler = ranking last_u (12s), exploration-B regular only. NOT a verdict.
import os; os.makedirs('./out', exist_ok=True)
import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
rk = pd.read_sql("SELECT snap_ms, symbol, last_u FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND duration='realtime' AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
ev = pd.read_parquet("./out/e1n10_origin.parquet")
ev["grp"]=np.where(ev.from_rank<=15,"flicker 11-15",np.where(ev.from_rank<=30,"16-30","arrival 31+/outside"))
paths = {s:g.sort_values("snap_ms") for s,g in rk.groupby("symbol")}
H=[60,300,900,1800]
rows=[]
for t0,sym,grp,band,px in zip(ev.t0,ev.symbol,ev.grp,ev.band,ev.px):
    p=paths[sym]; t=p.snap_ms.values; v=p.last_u.values/1e6
    i0=np.searchsorted(t,t0,side="right")     # first snap strictly after t0 (t0 snap itself is the entry snap; use next snap as entry price -> no lookahead)
    if i0>=len(t) or t[i0]-t0>60000: continue
    entry=v[i0]; r={"grp":grp,"band":band,"entry":entry}
    ok=True
    for h in H:
        j=np.searchsorted(t,t0+h*1000,side="left")
        if j>=len(t) or abs(t[j]-(t0+h*1000))>90000: r[f"ret_{h}"]=np.nan; continue
        seg=v[i0:j+1]
        r[f"ret_{h}"]=v[j]/entry-1; r[f"max_{h}"]=seg.max()/entry-1; r[f"min_{h}"]=seg.min()/entry-1
    rows.append(r)
d=pd.DataFrame(rows)
print("ruler: ranking last_u (~12.5s grid, 18s stale, symbol must remain in TOSS top-100). exploration-B regular. NOT a verdict.")
for grp,g in d.groupby("grp"):
    print(f"\n== {grp}: n={len(g)}  (with 30-min path: {g.ret_1800.notna().sum()})")
    tab=pd.DataFrame({h:{"ret_mean":g[f'ret_{h}'].mean(),"ret_p50":g[f'ret_{h}'].median(),"max_p50":g[f'max_{h}'].median(),"min_p50":g[f'min_{h}'].median(),"pos_frac":(g[f'ret_{h}']>0).mean(),"n":g[f'ret_{h}'].notna().sum()} for h in H}).T
    print((tab*[100,100,100,100,1,1]).round(2).to_string())
g=d[(d.grp=="arrival 31+/outside")&(d.band.isin(["$0-2","$2-5"]))]
print(f"\n== arrivals in $0-5 only: n={len(g)}")
print(pd.DataFrame({h:{"ret_mean%":g[f'ret_{h}'].mean()*100,"ret_p50%":g[f'ret_{h}'].median()*100,"max_p50%":g[f'max_{h}'].median()*100,"min_p50%":g[f'min_{h}'].median()*100,"pos_frac":(g[f'ret_{h}']>0).mean(),"n":g[f'ret_{h}'].notna().sum()} for h in H}).T.round(2).to_string())

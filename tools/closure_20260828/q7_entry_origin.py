import os; os.makedirs('./out', exist_ok=True)
import sqlite3, time, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
t=time.time()
rk = pd.read_sql("SELECT snap_ms, rank, symbol, last_u FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND duration='realtime' AND snap_ms>=? AND snap_ms<? ORDER BY snap_ms, rank", con, params=(A,B))
mm=((rk.snap_ms+9*3600*1000)%86400000)//60000
rk=rk[(mm>=1350)|(mm<300)]   # regular only
print(f"rows {len(rk)} [{time.time()-t:.0f}s]")
snaps=sorted(rk.snap_ms.unique()); idx={s:i for i,s in enumerate(snaps)}
rank_of = {s:dict(zip(g.symbol,g["rank"])) for s,g in rk.groupby("snap_ms")}
last_of = {s:dict(zip(g.symbol,g.last_u)) for s,g in rk.groupby("snap_ms")}
ev=[]
for i in range(1,len(snaps)):
    s0,s1=snaps[i-1],snaps[i]
    if s1-s0>60000: continue
    r0,r1=rank_of[s0],rank_of[s1]
    for sym,r in r1.items():
        if r<=10 and r0.get(sym,999)>10:
            # dwell
            k=0
            while i+k+1<len(snaps) and rank_of[snaps[i+k+1]].get(sym,999)<=10 and snaps[i+k+1]-snaps[i+k]<=60000: k+=1
            # was it in top100 during previous 30 min? (first appearance in list this session?)
            j=i-1; seen_recent=False
            while j>=0 and s1-snaps[j]<=1800000:
                if sym in rank_of[snaps[j]]: seen_recent=True; break
                j-=1
            ev.append(dict(t0=s1,symbol=sym,from_rank=r0.get(sym,999),to_rank=r,stay=k,px=last_of[s1][sym]/1e6,seen_30m=seen_recent))
ev=pd.DataFrame(ev)
ev["origin"]=pd.cut(ev.from_rank,[10,15,30,100,1000],labels=["11-15","16-30","31-100","outside top100"])
ev["band"]=pd.cut(ev.px,[0,2,5,20,1e9],labels=["$0-2","$2-5","$5-20","$20+"])
print("\nRegular-session TOSS top-10 entries by ORIGIN rank (prev snap):")
g=ev.groupby("origin",observed=True)
print(pd.DataFrame({"n":g.size(),"share":(g.size()/len(ev)).round(3),"stay_p50_snaps":g.stay.median(),"stay>=8_frac":g.stay.apply(lambda s:(s>=8).mean()).round(3),"not_in_list_prev30m":(1-g.seen_30m.mean()).round(3)}).to_string())
print("\nentries from outside top-100 (true arrivals) by band, per session:")
x=ev[ev.origin=="outside top100"]
x=x.assign(date=pd.to_datetime(x.t0,unit="ms",utc=True).dt.tz_convert("Asia/Seoul").dt.date)
print(pd.crosstab(x.date,x.band).to_string())
print("\ntrue arrivals: to_rank distribution:", x.to_rank.value_counts().sort_index().to_dict())
ev.to_parquet("./out/e1n10_origin.parquet")

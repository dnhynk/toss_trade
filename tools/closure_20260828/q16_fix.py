import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
rk = pd.read_sql("SELECT snap_ms, rank, symbol, last_u FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND duration='realtime' AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
rk["kst"]=pd.to_datetime(rk.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); rk["date"]=rk.kst.dt.date; rk["hm"]=rk.kst.dt.hour*60+rk.kst.dt.minute; rk["px"]=rk.last_u/1e6
paths={s:g[["snap_ms","px"]].drop_duplicates("snap_ms").sort_values("snap_ms") for s,g in rk.groupby("symbol")}
def px_at(sym,t,tol=240000):
    p=paths.get(sym); a=p.snap_ms.values; i=np.searchsorted(a,t); c=[j for j in (i-1,i) if 0<=j<len(a) and abs(a[j]-t)<=tol]
    return p.px.values[min(c,key=lambda j:abs(a[j]-t))] if c else np.nan
def T(date,minutes): base=dt.datetime.combine(date,dt.time(0,0),tzinfo=dt.timezone(dt.timedelta(hours=9))); return int((base+dt.timedelta(minutes=minutes)).timestamp()*1000)
CLOCK=[("D 09:05",9*60+5),("D 16:55",16*60+55),("D 17:05",17*60+5),("D 18:00",18*60),("D 22:25",22*60+25),("D 22:35",22*60+35),("D 23:30",23*60+30),("D+1 04:55",28*60+55),("D+1 05:05",29*60+5),("D+1 08:45",32*60+45),("D+1 09:05",33*60+5),("D+1 16:55",40*60+55)]
rows=[]
for date in sorted(rk.date.unique()):
    g=rk[(rk.date==date)&(rk.hm>=16*60+50)&(rk.hm<=16*60+55)&(rk["rank"]<=20)]
    for sym in g.symbol.unique():
        p0=px_at(sym,T(date,16*60+55))
        if not p0>0 or p0>=5: continue
        rows.append({"date":date,"symbol":sym,**{lab:px_at(sym,T(date,mn)) for lab,mn in CLOCK}})
d=pd.DataFrame(rows)
print("TOSS top-20 @16:55 anchor, $0-5, n=",len(d))
segs=[("D 09:05","D 16:55"),("D 16:55","D 17:05"),("D 17:05","D 18:00"),("D 18:00","D 22:25"),("D 22:25","D 22:35"),("D 22:35","D 23:30"),("D 23:30","D+1 04:55"),("D+1 04:55","D+1 05:05"),("D+1 05:05","D+1 08:45"),("D+1 08:45","D+1 09:05"),("D+1 09:05","D+1 16:55"),("D 16:55","D+1 16:55")]
for a_,b_ in segs:
    r=(d[b_]/d[a_]-1).dropna()
    print(f"  {a_:>10} -> {b_:<10}  median {r.median()*100:+6.2f}%  mean {r.mean()*100:+6.2f}%  pos {(r>0).mean():.2f}  n={len(r)}")
# ex-ante day-session: TOSS top-20 at 09:30, $0-5
rows=[]
for date in sorted(rk.date.unique()):
    g=rk[(rk.date==date)&(rk.hm>=9*60+25)&(rk.hm<=9*60+30)&(rk["rank"]<=20)]
    for sym in g.symbol.unique():
        p0=px_at(sym,T(date,9*60+30))
        if not p0>0 or p0>=5: continue
        rows.append({"date":date,"symbol":sym,"r_1655":px_at(sym,T(date,16*60+55))/p0-1,"r_1800":px_at(sym,T(date,18*60))/p0-1})
e=pd.DataFrame(rows)
for c in ("r_1655","r_1800"):
    r=e[c].dropna(); print(f"ex-ante (09:30 anchor) {c}: median {r.median()*100:+.2f}% mean {r.mean()*100:+.2f}% pos {(r>0).mean():.2f} n={len(r)}  per-date median:", (e.groupby('date')[c].median()*100).round(1).to_dict())

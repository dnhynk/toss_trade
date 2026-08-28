# 24h clock of Toss-favoured $0-5 names vs US-driven control. Anchor = TOSS top-20 at 16:55 KST day D (or MKT-only top-20). Ruler = ranking last_u (any of the two lists).
import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
rk = pd.read_sql("SELECT snap_ms, ranking_type, rank, symbol, last_u FROM rankings_snap WHERE ranking_type IN ('TOSS_SECURITIES_TRADING_VOLUME','MARKET_TRADING_VOLUME') AND duration='realtime' AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
rk["kst"]=pd.to_datetime(rk.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); rk["date"]=rk.kst.dt.date; rk["hm"]=rk.kst.dt.hour*60+rk.kst.dt.minute; rk["px"]=rk.last_u/1e6
paths={s:g[["snap_ms","px"]].drop_duplicates("snap_ms").sort_values("snap_ms") for s,g in rk.groupby("symbol")}
def px_at(sym,t,tol=240000):
    p=paths.get(sym); a=p.snap_ms.values; i=np.searchsorted(a,t); c=[j for j in (i-1,i) if 0<=j<len(a) and abs(a[j]-t)<=tol]
    return p.px.values[min(c,key=lambda j:abs(a[j]-t))] if c else np.nan
def T(date,minutes): base=dt.datetime.combine(date,dt.time(0,0),tzinfo=dt.timezone(dt.timedelta(hours=9))); return int((base+dt.timedelta(minutes=minutes)).timestamp()*1000)
CLOCK=[("D 09:05",9*60+5),("D 12:00",12*60),("D 16:55",16*60+55),("D 17:05",17*60+5),("D 18:00",18*60),("D 20:00",20*60),("D 22:25",22*60+25),("D 22:35",22*60+35),("D 23:30",23*60+30),("D+1 01:00",25*60),("D+1 03:00",27*60),("D+1 04:55",28*60+55),("D+1 05:05",29*60+5),("D+1 07:00",31*60),("D+1 08:45",32*60+45),("D+1 09:05",33*60+5),("D+1 12:00",36*60),("D+1 16:55",40*60+55)]
def run(label, pick):
    rows=[]
    for date in sorted(rk.date.unique()):
        g=rk[(rk.date==date)&(rk.hm>=16*60+50)&(rk.hm<=16*60+55)]
        toss=set(g[(g.ranking_type=='TOSS_SECURITIES_TRADING_VOLUME')&(g["rank"]<=20)].symbol); mkt=set(g[(g.ranking_type=='MARKET_TRADING_VOLUME')&(g["rank"]<=20)].symbol)
        for sym in pick(toss,mkt):
            p0=px_at(sym,T(date,16*60+55))
            if not p0>0 or p0>=5: continue
            rows.append({"date":date,"symbol":sym,**{lab:px_at(sym,T(date,mn))/p0-1 for lab,mn in CLOCK}})
    d=pd.DataFrame(rows)
    print(f"\n===== {label}: $0-5, n={len(d)} names over {d.date.nunique()} anchor dates. median % vs D 16:55 (n with data) =====")
    med=d.drop(columns=["date","symbol"]).median()*100; n=d.drop(columns=["date","symbol"]).notna().sum(); pos=(d.drop(columns=["date","symbol"])>0).mean()
    print(pd.DataFrame({"median%":med.round(2),"pos_frac":pos.round(2),"n":n}).to_string())
    return d
d=run("TOSS top-20 at 16:55 (Toss-favoured)", lambda t,m:t)
c=run("MKT top-20 but NOT TOSS top-20 (US-driven control)", lambda t,m:m-t)
# segment returns (median of per-name segment returns) for the Toss group
print("\n===== TOSS group: per-name SEGMENT returns (median %, pos_frac) =====")
segs=[("D 16:55","D 17:05"),("D 17:05","D 18:00"),("D 18:00","D 22:25"),("D 22:25","D 22:35"),("D 22:35","D 23:30"),("D 23:30","D+1 04:55"),("D+1 04:55","D+1 05:05"),("D+1 05:05","D+1 08:45"),("D+1 08:45","D+1 09:05"),("D+1 09:05","D+1 16:55")]
for a_,b_ in segs:
    r=(1+d[b_])/(1+d[a_])-1
    print(f"  {a_:>10} -> {b_:<10}  median {r.median()*100:+6.2f}%  pos {(r>0).mean():.2f}  n={r.notna().sum()}")
print("\nper-date median of (D 18:00 / D 16:55 - 1), TOSS group:", (d.groupby('date')['D 18:00'].median()*100).round(1).to_dict())
print("per-date median of (D+1 04:55 / D 23:30 - 1), TOSS group:", ((1+d['D+1 04:55'])/(1+d['D 23:30'])-1).groupby(d.date).median().mul(100).round(1).to_dict())

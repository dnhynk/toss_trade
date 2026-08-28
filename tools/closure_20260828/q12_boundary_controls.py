# Controls for the session-boundary fade: (a) split by prior-session return sign, (b) MARKET-top-but-not-TOSS-top names, (c) per-date consistency, (d) intraday day-session path
import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
rk = pd.read_sql("SELECT snap_ms, ranking_type, rank, symbol, last_u FROM rankings_snap WHERE ranking_type IN ('TOSS_SECURITIES_TRADING_VOLUME','MARKET_TRADING_VOLUME') AND duration='realtime' AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
rk["kst"]=pd.to_datetime(rk.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); rk["date"]=rk.kst.dt.date; rk["hm"]=rk.kst.dt.hour*60+rk.kst.dt.minute; rk["px"]=rk.last_u/1e6
allp = rk.sort_values("snap_ms")
paths={s:g[["snap_ms","px"]].drop_duplicates("snap_ms").sort_values("snap_ms") for s,g in allp.groupby("symbol")}
def px_at(sym,t,tol=180000):
    p=paths.get(sym)
    if p is None: return np.nan
    a=p.snap_ms.values; i=np.searchsorted(a,t); c=[j for j in (i-1,i) if 0<=j<len(a) and abs(a[j]-t)<=tol]
    return p.px.values[min(c,key=lambda j:abs(a[j]-t))] if c else np.nan
def sel(date,hm0,hm1,rtype,top=20):
    g=rk[(rk.date==date)&(rk.hm>=hm0)&(rk.hm<=hm1)&(rk.ranking_type==rtype)&(rk["rank"]<=top)]
    return set(g.symbol)
def T(date,hm_): 
    base=dt.datetime.combine(date,dt.time(0,0),tzinfo=dt.timezone(dt.timedelta(hours=9))); return int((base+dt.timedelta(minutes=hm_)).timestamp()*1000)
dates=sorted(rk.date.unique())
def study(name, t_prev, t_ref, t_open, t_1h):
    rows=[]
    for date in dates:
        toss=sel(date,t_ref-5,t_ref,'TOSS_SECURITIES_TRADING_VOLUME'); mkt=sel(date,t_ref-5,t_ref,'MARKET_TRADING_VOLUME')
        for sym in toss|mkt:
            p0=px_at(sym,T(date,t_ref)); pp=px_at(sym,T(date,t_prev)); po=px_at(sym,T(date,t_open)); p1=px_at(sym,T(date,t_1h))
            if not p0>0: continue
            rows.append(dict(date=date,symbol=sym,grp=("TOSS&MKT" if sym in toss and sym in mkt else "TOSS only" if sym in toss else "MKT only"),
                             prev_ret=pp/p0-1 if pp>0 else np.nan, open_ret=po/p0-1 if po>0 else np.nan, r1h=p1/p0-1 if p1>0 else np.nan, band="$0-5" if p0<5 else "$5+"))
    d=pd.DataFrame(rows); d["prior"]=np.where(d.prev_ret.isna(),"n/a",np.where(d.prev_ret<0,"rose in prior 2h",np.where(d.prev_ret>0,"fell in prior 2h","flat")))
    print(f"\n===== {name} =====")
    print("(a) by group x band: median open_ret / +1h ret, n")
    g=d.groupby(["grp","band"]); print(pd.DataFrame({"n":g.size(),"open_p50":g.open_ret.median()*100,"r1h_p50":g.r1h.median()*100,"r1h_pos":g.r1h.apply(lambda s:(s>0).mean())}).round(2).to_string())
    print("(b) TOSS-listed $0-5 by prior 2h move (regression-to-mean check):")
    x=d[(d.grp!="MKT only")&(d.band=="$0-5")]; g=x.groupby("prior"); print(pd.DataFrame({"n":g.size(),"prev_p50":g.prev_ret.median()*100,"open_p50":g.open_ret.median()*100,"r1h_p50":g.r1h.median()*100,"r1h_pos":g.r1h.apply(lambda s:(s>0).mean())}).round(2).to_string())
    print("(c) per-date median +1h ret, TOSS-listed $0-5:")
    print((x.groupby("date").r1h.median()*100).round(2).to_dict())
study("day->pre  (prev=14:55, ref=16:55, open=17:05, 1h=18:00)", 14*60+55, 16*60+55, 17*60+5, 18*60)
study("pre->regular (prev=20:25, ref=22:25, open=22:35, 1h=23:30)", 20*60+25, 22*60+25, 22*60+35, 23*60+30)
study("after->day (prev=06:45, ref=08:45, open=09:05, 1h=10:00)", 6*60+45, 8*60+45, 9*60+5, 10*60)
# (d) intraday day-session path of names that end in TOSS top-20 at 16:55 vs names in TOSS top-20 at 09:30
print("\n===== (d) day-session path, TOSS top-20 at 09:30, $0-5: median ret vs 09:30 price =====")
rows=[]
for date in dates:
    for sym in sel(date,9*60+25,9*60+30,'TOSS_SECURITIES_TRADING_VOLUME'):
        p0=px_at(sym,T(date,9*60+30))
        if not p0>0 or p0>=5: continue
        rows.append({"date":date,"symbol":sym, **{f"h{h}":px_at(sym,T(date,h))/p0-1 for h in (10*60,11*60,13*60,15*60,16*60+55,17*60+30,18*60)}})
d=pd.DataFrame(rows); print("n=",len(d)); print((d.drop(columns=["date","symbol"]).median()*100).round(2).to_string())

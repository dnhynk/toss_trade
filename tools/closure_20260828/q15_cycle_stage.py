import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-11"), ms("2026-08-26")   # one extra week of history only to know 'days in top-20' (no forward use before 08-18)
rk = pd.read_sql("SELECT snap_ms, rank, symbol, last_u FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND duration='realtime' AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
rk["kst"]=pd.to_datetime(rk.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); rk["date"]=rk.kst.dt.date; rk["hm"]=rk.kst.dt.hour*60+rk.kst.dt.minute; rk["px"]=rk.last_u/1e6
paths={s:g[["snap_ms","px"]].drop_duplicates("snap_ms").sort_values("snap_ms") for s,g in rk.groupby("symbol")}
def px_at(sym,t,tol=240000):
    p=paths.get(sym); a=p.snap_ms.values; i=np.searchsorted(a,t); c=[j for j in (i-1,i) if 0<=j<len(a) and abs(a[j]-t)<=tol]
    return p.px.values[min(c,key=lambda j:abs(a[j]-t))] if c else np.nan
def T(date,minutes): base=dt.datetime.combine(date,dt.time(0,0),tzinfo=dt.timezone(dt.timedelta(hours=9))); return int((base+dt.timedelta(minutes=minutes)).timestamp()*1000)
top20_days = rk[rk["rank"]<=20].groupby("symbol").date.apply(lambda s: sorted(set(s))).to_dict()
rows=[]
for date in sorted(d for d in rk.date.unique() if d>=dt.date(2026,8,18)):
    g=rk[(rk.date==date)&(rk.hm>=16*60+50)&(rk.hm<=16*60+55)&(rk["rank"]<=20)]
    for sym in g.symbol.unique():
        p0=px_at(sym,T(date,16*60+55))
        if not p0>0 or p0>=5: continue
        prior=[d for d in top20_days[sym] if d<date and (date-d).days<=7]
        rows.append(dict(date=date,symbol=sym,stage=("day1 (no top-20 in prior 7d)" if not prior else "repeat"),
            r_18=px_at(sym,T(date,18*60))/p0-1, r_2330=px_at(sym,T(date,23*60+30))/p0-1, r_0455=px_at(sym,T(date,28*60+55))/p0-1, r_next_day=px_at(sym,T(date,40*60+55))/p0-1))
d=pd.DataFrame(rows)
print(f"TOSS top-20 at 16:55, $0-5, 08-18..08-25: n={len(d)}")
for col in ("r_18","r_2330","r_0455","r_next_day"):
    print(f"\n{col}: vs D 16:55")
    print(d.groupby("stage")[col].describe(percentiles=[.1,.25,.5,.75,.9])[["count","mean","10%","25%","50%","75%","90%"]].mul([1,100,100,100,100,100,100]).round(2).to_string())
print("\nALL: mean / median / p90 of 24h return:", round(d.r_next_day.mean()*100,2), round(d.r_next_day.median()*100,2), round(d.r_next_day.quantile(.9)*100,2), "  n=", d.r_next_day.notna().sum())
print("stage counts:", d.stage.value_counts().to_dict())

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
rows=[]
for date in sorted(rk.date.unique()):
    g=rk[(rk.date==date)&(rk.hm>=9*60+25)&(rk.hm<=9*60+30)&(rk["rank"]<=20)]   # ex-ante anchor: top-20 at 09:30
    for sym in g.symbol.unique():
        p_1705=px_at(sym,T(date,17*60+5))
        if not p_1705>0 or p_1705>=5: continue
        rows.append(dict(date=date,symbol=sym,
            s_1800=px_at(sym,T(date,18*60))/p_1705-1, s_2225=px_at(sym,T(date,22*60+25))/p_1705-1,
            s_2330=px_at(sym,T(date,23*60+30))/p_1705-1, s_0455=px_at(sym,T(date,28*60+55))/p_1705-1,
            worst_up=np.nan))
d=pd.DataFrame(rows)
# max adverse excursion for a short entered 17:05: highest price seen until 04:55 next day
for i,r in d.iterrows():
    p=paths[r.symbol]; s=p[(p.snap_ms>=T(r.date,17*60+5))&(p.snap_ms<=T(r.date,28*60+55))]
    d.loc[i,"worst_up"]=s.px.max()/px_at(r.symbol,T(r.date,17*60+5))-1 if len(s) else np.nan
print("SHORT entered at 17:05 KST (after the venue step), ex-ante 09:30 top-20 anchor, $0-5, n=",len(d))
for c,lab in (("s_1800","-> 18:00"),("s_2225","-> 22:25 (pre close)"),("s_2330","-> 23:30"),("s_0455","-> next 04:55 (reg close)")):
    r=d[c].dropna()
    print(f"  {lab:28} median {r.median()*100:+6.2f}%  mean {r.mean()*100:+6.2f}%  frac<0 {(r<0).mean():.2f}  n={len(r)}   per-date median:", {str(k)[5:]:round(v*100,1) for k,v in (d.groupby('date')[c].median()).items()})
w=d.worst_up.dropna()
print(f"\n  max adverse excursion (short 17:05 -> 04:55): median {w.median()*100:+.1f}%  p75 {w.quantile(.75)*100:+.1f}%  p90 {w.quantile(.9)*100:+.1f}%  max {w.max()*100:+.1f}%  n={len(w)}")
print("  names that rose >10% at some point vs 17:05:", int((w>0.10).sum()), " / >25%:", int((w>0.25).sum()))

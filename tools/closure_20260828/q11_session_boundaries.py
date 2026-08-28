# Session-boundary behaviour of Toss-favoured names (ranking last_u ruler). Exploration-B era. Structural, not a verdict.
import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
rk = pd.read_sql("SELECT snap_ms, rank, symbol, last_u, vol_qu FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND duration='realtime' AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
rk["kst"]=pd.to_datetime(rk.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul")
rk["date"]=rk.kst.dt.date; rk["hm"]=rk.kst.dt.hour*60+rk.kst.dt.minute
rk["px"]=rk.last_u/1e6
paths={s:g.sort_values("snap_ms") for s,g in rk.groupby("symbol")}
def px_at(sym,t,tol=180000):
    p=paths[sym]; i=np.searchsorted(p.snap_ms.values,t)
    cands=[j for j in (i-1,i) if 0<=j<len(p) and abs(p.snap_ms.values[j]-t)<=tol]
    if not cands: return np.nan
    j=min(cands,key=lambda j:abs(p.snap_ms.values[j]-t)); return p.px.values[j]
def hi_lo(sym,t1,t2):
    p=paths[sym]; s=p[(p.snap_ms>=t1)&(p.snap_ms<=t2)]
    return (s.px.max(), s.px.min(), len(s)) if len(s) else (np.nan,np.nan,0)
BOUND={"after->day (08:45 -> 09:05/09:30/10:00 KST)":(8*60+45, 9*60+5, [9*60+30, 10*60], (9*60, 10*60)),
       "day->pre (16:55 -> 17:05/17:30/18:00)":(16*60+55, 17*60+5, [17*60+30, 18*60], (17*60, 18*60)),
       "pre->regular (22:25 -> 22:35/23:00/23:30)":(22*60+25, 22*60+35, [23*60, 23*60+30], (22*60+30, 23*60+30))}
for name,(t_ref,t_open,later,(w1,w2)) in BOUND.items():
    rows=[]
    for date,g in rk.groupby("date"):
        ref=g[(g.hm>=t_ref-5)&(g.hm<=t_ref)&(g["rank"]<=20)]
        if len(ref)==0: continue
        base=dt.datetime.combine(date,dt.time(0,0),tzinfo=dt.timezone(dt.timedelta(hours=9)))
        T=lambda hm_: int((base+dt.timedelta(minutes=hm_)).timestamp()*1000)
        for sym in ref.symbol.unique():
            p0=px_at(sym,T(t_ref))
            if not p0>0: continue
            r={"date":date,"symbol":sym,"p0":p0}
            r["open"]=px_at(sym,T(t_open))/p0-1
            for k,hm_ in enumerate(later): r[f"later{k}"]=px_at(sym,T(hm_))/p0-1
            hi,lo,n=hi_lo(sym,T(w1),T(w2)); r["hi_1h"]=hi/p0-1; r["lo_1h"]=lo/p0-1; r["n_1h"]=n
            r["band"]="$0-2" if p0<2 else "$2-5" if p0<5 else "$5-20" if p0<20 else "$20+"
            rows.append(r)
    d=pd.DataFrame(rows)
    print(f"\n=== {name}: TOSS top-20 names at boundary, n={len(d)} over {d.date.nunique()} dates ===")
    for b,gg in list(d.groupby("band"))+[("ALL",d)]:
        print(f"  {b:5} n={len(gg):3}  open: mean {gg.open.mean()*100:+.2f}% p50 {gg.open.median()*100:+.2f}% pos {(gg.open>0).mean():.2f} | +30m p50 {gg.later0.median()*100:+.2f}% | +1h p50 {gg.later1.median()*100:+.2f}% (n={gg.later1.notna().sum()}) | 1h hi p50 {gg.hi_1h.median()*100:+.2f}% lo p50 {gg.lo_1h.median()*100:+.2f}%")
# concentration of Toss top-10 per regular session
reg=rk[(rk.hm>=22*60+30)|(rk.hm<5*60)]
reg=reg.assign(sdate=np.where(reg.hm<5*60, (pd.to_datetime(reg.date)-pd.Timedelta(days=1)).dt.date, reg.date))
print("\n=== regular sessions: distinct symbols ever in TOSS top-10 / top-3 ===")
print(reg[reg["rank"]<=10].groupby("sdate").symbol.nunique().to_dict())
print(reg[reg["rank"]<=3].groupby("sdate").symbol.nunique().to_dict())

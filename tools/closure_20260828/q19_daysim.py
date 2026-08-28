# Phase 0: pessimistic queue-aware simulation of two-sided passive quoting in the Toss DAY session (09:00-16:50 KST).
# Rules (frozen before running):
#  - one clip per name; bid joins at bid1 with queue_ahead = bid1_qu at join; priority lost whenever bid1 moves up (re-join)
#  - bid fills if (a) bid1 moves BELOW our price (traded through -> adverse fill at our price) or (b) printed volume at price<=our bid since join >= queue_ahead + our qty
#  - after buy at Pb: ask posted at ask1 only when ask1 >= Pb + 1 tick (variant E1) ; fills symmetric ((a) ask1 moves above ours, (b) volume at price>=ours >= queue+qty)
#  - inventory older than MAX_HOLD or at 16:50 or at end of tape coverage -> liquidate at bid1 (cross)
#  - commission 0.1% per side. Tape is a 50-cap sample (undercounts prints -> pessimistic on fills)
import os; os.makedirs('./out', exist_ok=True)
import sqlite3, datetime as dt, json, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
ob = pd.read_sql("SELECT symbol, snap_ms, bid1_u, bid1_qu, ask1_u, ask1_qu FROM orderbook_snap WHERE snap_ms>=? AND snap_ms<? AND bid1_u>0 AND ask1_u>bid1_u", con, params=(A,B))
tr = pd.read_sql("SELECT symbol, ts_ms, price_u, qty_u FROM trades_snap WHERE ts_ms>=? AND ts_ms<?", con, params=(A,B))
KST=9*3600*1000
def hm(x): return ((x+KST)%86400000)//60000
ob=ob[(hm(ob.snap_ms)>=540)&(hm(ob.snap_ms)<1010)].copy(); tr=tr[(hm(tr.ts_ms)>=540)&(hm(tr.ts_ms)<1010)].copy()
ob["date"]=((ob.snap_ms+KST)//86400000); tr["date"]=((tr.ts_ms+KST)//86400000)
import sys
GAP=int(sys.argv[1])*1000 if len(sys.argv)>1 else 300000; MIN_MIN=20
CLIPS=[100,200,500]; MAX_HOLD=60*60*1000; COMM=0.001
def tick(p): return 0.0001 if p<1 else 0.01
results=[]
for (sym,date),bk in ob.groupby(["symbol","date"]):
    bk=bk.sort_values("snap_ms").reset_index(drop=True)
    # split into contiguous coverage windows (gap > 60s breaks)
    gaps=np.where(np.diff(bk.snap_ms.values)>GAP)[0]; starts=[0]+list(gaps+1); ends=list(gaps+1)+[len(bk)]
    t_sym=tr[(tr.symbol==sym)&(tr.date==date)].sort_values("ts_ms")
    for s0,s1 in zip(starts,ends):
        w=bk.iloc[s0:s1]
        if (w.snap_ms.iloc[-1]-w.snap_ms.iloc[0])<MIN_MIN*60000: continue
        tw=t_sym[(t_sym.ts_ms>=w.snap_ms.iloc[0])&(t_sym.ts_ms<=w.snap_ms.iloc[-1])]
        for clip in CLIPS:
            pos=None; bid=None; ask=None; trades=[]; t_end=w.snap_ms.iloc[-1]; buys=0
            rows=list(zip(w.snap_ms.values,w.bid1_u.values/1e6,w.bid1_qu.values/1e6,w.ask1_u.values/1e6,w.ask1_qu.values/1e6))
            for i,(t,b1,bq,a1,aq) in enumerate(rows):
                t_prev=rows[i-1][0] if i else t
                seg=tw[(tw.ts_ms>t_prev)&(tw.ts_ms<=t)]
                if pos is None:
                    if bid is None or b1>bid["px"]+1e-9:          # (re)join at bid1 -> lose priority
                        bid={"px":b1,"q_ahead":bq,"qty":clip/b1,"vol":0.0,"t":t}
                    else:
                        if b1<bid["px"]-1e-9:                        # traded through -> adverse fill
                            pos={"px":bid["px"],"qty":bid["qty"],"t":t}; bid=None; buys+=1; continue
                        bid["vol"]+=seg.loc[seg.price_u/1e6<=bid["px"]+1e-9,"qty_u"].sum()/1e6
                        if bid["vol"]>=bid["q_ahead"]+bid["qty"]:
                            pos={"px":bid["px"],"qty":bid["qty"],"t":t}; bid=None; buys+=1; continue
                else:
                    tk=tick(pos["px"]); forced = (t-pos["t"]>MAX_HOLD) or (i==len(rows)-1)
                    if forced:                                       # liquidate at bid1 (cross)
                        trades.append(dict(sym=sym,date=date,clip=clip,pb=pos["px"],ps=b1,forced=True,hold_s=(t-pos["t"])/1000)); pos=None; ask=None; continue
                    if a1<pos["px"]+tk-1e-9:                         # not willing to sell below +1 tick
                        ask=None; continue
                    if ask is None or a1<ask["px"]-1e-9:             # (re)join at ask1 when it comes down to us / first time
                        ask={"px":a1,"q_ahead":aq,"qty":pos["qty"],"vol":0.0}
                    else:
                        if a1>ask["px"]+1e-9:                        # traded through upward -> filled at our ask
                            trades.append(dict(sym=sym,date=date,clip=clip,pb=pos["px"],ps=ask["px"],forced=False,hold_s=(t-pos["t"])/1000)); pos=None; ask=None; continue
                        ask["vol"]+=seg.loc[seg.price_u/1e6>=ask["px"]-1e-9,"qty_u"].sum()/1e6
                        if ask["vol"]>=ask["q_ahead"]+ask["qty"]:
                            trades.append(dict(sym=sym,date=date,clip=clip,pb=pos["px"],ps=ask["px"],forced=False,hold_s=(t-pos["t"])/1000)); pos=None; ask=None; continue
            results.append(dict(sym=sym,date=date,clip=clip,window_h=(t_end-w.snap_ms.iloc[0])/3.6e6,mid=float(np.median(w.bid1_u+w.ask1_u))/2e6,buys=buys,trades=trades))
R=pd.DataFrame(results)
T=pd.DataFrame([t for ts in R.trades for t in ts])
T["ret"]=T.ps/T.pb-1-2*COMM
T["band"]=pd.cut(T.pb,[0,1,2,5,20,1e9],labels=["<$1","$1-2","$2-5","$5-20","$20+"])
R["band"]=pd.cut(R.mid,[0,1,2,5,20,1e9],labels=["<$1","$1-2","$2-5","$5-20","$20+"])
print(f"GAP={GAP//1000}s MIN={MIN_MIN}min | windows: {R[R["clip"]==200].shape[0]} symbol-windows (>=30min), total coverage {R[R["clip"]==200].window_h.sum():.0f} name-hours, names {R.sym.nunique()}, dates {R.date.nunique()}")
for clip in CLIPS:
    t=T[T["clip"]==clip]; r=R[R["clip"]==clip]
    print(f"\n=== clip ${clip}: round trips {len(t)}  per name-hour {len(t)/r.window_h.sum():.2f}  forced-liquidation share {t.forced.mean():.2f}  hold p50 {t.hold_s.median()/60:.0f} min")
    g=t.groupby("band",observed=True)
    out=pd.DataFrame({"rt":g.size(),"ret_mean%":g.ret.mean()*100,"ret_p50%":g.ret.median()*100,"win":g.ret.apply(lambda s:(s>0).mean()),"forced":g.forced.mean(),
                      "forced_ret%":t[t.forced].groupby("band",observed=True).ret.mean()*100,"passive_ret%":t[~t.forced].groupby("band",observed=True).ret.mean()*100,
                      "name_hours":r.groupby("band",observed=True).window_h.sum()}).round(3)
    out["$/name-hour"]=(out.rt*out["ret_mean%"]/100*clip/out.name_hours).round(2)
    print(out.to_string())
t=T[T["clip"]==200]
print("\nclip $200 by date: net $ per name-hour")
for d,g in t.groupby("date"):
    nh=R[(R["clip"]==200)&(R.date==d)].window_h.sum(); print(f"  {dt.datetime.utcfromtimestamp(d*86400).date()}  rt={len(g):3}  net=${(g.ret*200).sum():+.1f}  name-hours={nh:.1f}  => ${(g.ret*200).sum()/nh:+.2f}/name-hour")
T.to_parquet("./out/daysim_trades.parquet"); R.drop(columns="trades").to_parquet("./out/daysim_windows.parquet")

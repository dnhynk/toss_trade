# Passive liquidity provision economics: markout after a print at bid (MM bought) / at ask (MM sold)
import os; os.makedirs('./out', exist_ok=True)
import sqlite3, time, datetime as dt, numpy as np, pandas as pd
SP="./out/"
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
ob = pd.read_sql("SELECT symbol, snap_ms, bid1_u, ask1_u FROM orderbook_snap WHERE snap_ms>=? AND snap_ms<? AND bid1_u>0 AND ask1_u>0 AND ask1_u>bid1_u", con, params=(A,B)).sort_values("snap_ms")
ob["mid"]=(ob.bid1_u+ob.ask1_u)/2
m = pd.read_parquet(SP+"tape_side.parquet")
m = m[m.side!="inside"].copy()
def sessv(ms_):
    mm = ((ms_ + 9*3600*1000) % 86400000) // 60000
    return np.select([(mm>=540)&(mm<1020),(mm>=1020)&(mm<1350),(mm>=1350)|(mm<300),(mm>=300)&(mm<530)],["day","pre","regular","after"],"closed")
# snapshot-weighted quoted spread for contrast
ob["sess"]=sessv(ob.snap_ms.values); ob["band"]=pd.cut(ob.mid/1e6,[0,2,5,20,1e9],labels=["$0-2","$2-5","$5-20","$20+"])
ob["rs"]=(ob.ask1_u-ob.bid1_u)/ob.mid
print("=== Quoted spread: SNAPSHOT-weighted (all book snaps) vs TRADE-weighted (from q2) - regular ===")
sw = ob[ob.sess=="regular"].groupby("band",observed=True).rs.median()
tw = m[m.sess=="regular"].groupby("band",observed=True).rel_spread.median()
print(pd.DataFrame({"snapshot_wtd_p50":sw,"trade_wtd_p50":tw}).round(4).to_string())
# markouts
ob2 = ob[["symbol","snap_ms","mid"]].rename(columns={"snap_ms":"t_fwd","mid":"mid_fwd"})
res={}
for tau in (30,60,300):
    x = m[["symbol","ts_ms","price_u","bid1_u","ask1_u","side","sess","band","qty_u"]].copy()
    x["t_target"]=x.ts_ms+tau*1000
    x=x.sort_values("t_target")
    x = pd.merge_asof(x, ob2.sort_values("t_fwd"), left_on="t_target", right_on="t_fwd", by="symbol", direction="forward", tolerance=15000).dropna(subset=["mid_fwd"])
    # MM economics: at bid print, MM bought at bid1 -> pnl = mid_fwd - bid1 ; at ask print, MM sold at ask1 -> pnl = ask1 - mid_fwd
    x["mm_pnl"]=np.where(x.side=="SELL@bid-", (x.mid_fwd-x.bid1_u)/x.bid1_u, (x.ask1_u-x.mid_fwd)/x.ask1_u)
    x["half_spread"]=(x.ask1_u-x.bid1_u)/2/((x.ask1_u+x.bid1_u)/2)
    x["adverse"]=x.half_spread-x.mm_pnl   # how much of the half-spread is lost to adverse selection
    res[tau]=x
    g=x.groupby(["sess","band"],observed=True)
    out=pd.DataFrame({"n":g.size(),"half_spread_p50":g.half_spread.median(),"mm_pnl_mean":g.mm_pnl.mean(),"mm_pnl_p50":g.mm_pnl.median(),"win_rate":g.mm_pnl.apply(lambda s:(s>0).mean())})
    print(f"\n=== Markout tau={tau}s: MM P&L per passive fill (after earning half-spread), by session x band ===")
    print(out.round(4).to_string())
# side split, regular, tau=60
x=res[60]; r=x[x.sess=="regular"]
print("\n=== tau=60 regular: by side (bid-fill = MM bought / ask-fill = MM sold) ===")
print(r.groupby(["band","side"],observed=True).mm_pnl.agg(["size","mean","median"]).round(4).to_string())
res[60].to_parquet(SP+"markout60.parquet")

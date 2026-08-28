import os; os.makedirs('./out', exist_ok=True)
import sqlite3, time, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")   # exploration-B era only
t=time.time()
ob = pd.read_sql("SELECT symbol, snap_ms, bid1_u, bid1_qu, ask1_u, ask1_qu FROM orderbook_snap WHERE snap_ms>=? AND snap_ms<? AND bid1_u>0 AND ask1_u>0 AND ask1_u>bid1_u", con, params=(A,B))
tr = pd.read_sql("SELECT symbol, ts_ms, price_u, qty_u FROM trades_snap WHERE ts_ms>=? AND ts_ms<?", con, params=(A,B))
print(f"ob {len(ob)} tr {len(tr)} [{time.time()-t:.0f}s]")
def sess(ms_):
    m = ((ms_ + 9*3600*1000) % 86400000) // 60000
    return np.select([(m>=540)&(m<1020),(m>=1020)&(m<1350),(m>=1350)|(m<300),(m>=300)&(m<530)],["day","pre","regular","after"],"closed")
ob["sess"]=sess(ob.snap_ms.values); tr["sess"]=sess(tr.ts_ms.values)
ob=ob.sort_values("snap_ms"); tr=tr.sort_values("ts_ms")
m = pd.merge_asof(tr, ob.drop(columns="sess"), left_on="ts_ms", right_on="snap_ms", by="symbol", direction="backward", tolerance=10000)
m = m.dropna(subset=["bid1_u"])
m["mid"]=(m.bid1_u+m.ask1_u)/2; m["px"]=m.price_u/1e6
m["band"]=pd.cut(m.px,[0,2,5,20,1e9],labels=["$0-2","$2-5","$5-20","$20+"])
m["side"]=np.select([m.price_u>=m.ask1_u, m.price_u<=m.bid1_u],["BUY@ask+","SELL@bid-"],"inside")
m["rel_spread"]=(m.ask1_u-m.bid1_u)/m.mid
m["notional"]=m.px*m.qty_u/1e6
print(f"trades matched to a book <=10s old: {len(m)} of {len(tr)} ({len(m)/len(tr):.0%})")
print("\n=== Print location (share of trades) by session x band, exploration-B era ===")
g = m.groupby(["sess","band"],observed=True)
out = g.side.value_counts(normalize=True).unstack().fillna(0)
out["n"]=g.size(); out["qspread_p50"]=g.rel_spread.median(); out["notional_p50"]=g.notional.median()
print(out.round(3).to_string())
# Buy-initiated *volume* share
m["is_buy"]=(m.side=="BUY@ask+").astype(int); m["is_sell"]=(m.side=="SELL@bid-").astype(int)
print("\n=== Regular session: buy-initiated share of classified VOLUME, by band ===")
r = m[m.sess=="regular"]
for b,gg in r.groupby("band",observed=True):
    bq=gg.loc[gg.is_buy==1,"qty_u"].sum(); sq=gg.loc[gg.is_sell==1,"qty_u"].sum()
    print(f"  {b:6} buy_vol_share={bq/(bq+sq):.3f}  n_classified={int((gg.is_buy+gg.is_sell).sum())}")
m.to_parquet("./out/tape_side.parquet")

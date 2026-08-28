import os; os.makedirs('./out', exist_ok=True)
import numpy as np, pandas as pd
m = pd.read_parquet("./out/tape_side.parquet").sort_values("ts_ms")
ev = pd.read_parquet("./out/e1n10_origin.parquet")   # regular only, has from_rank, stay
by_sym = {s:g for s,g in m.groupby("symbol")}
rows=[]
for t0,sym,band,stay,fr in zip(ev.t0,ev.symbol,ev.band,ev.stay,ev.from_rank):
    g=by_sym.get(sym)
    if g is None: continue
    pre=g[(g.ts_ms>=t0-600000)&(g.ts_ms<t0-300000)]; pre2=g[(g.ts_ms>=t0-300000)&(g.ts_ms<t0)]; post=g[(g.ts_ms>t0)&(g.ts_ms<=t0+300000)]
    if len(pre2)<5 or len(post)<5: continue
    def bs(d):
        b=d.loc[d.side=="BUY@ask+","qty_u"].sum(); s=d.loc[d.side=="SELL@bid-","qty_u"].sum()
        return b/(b+s) if b+s>0 else np.nan
    rows.append(dict(symbol=sym,band=band,stay=stay,from_rank=fr,n_pre=len(pre),n_pre2=len(pre2),n_post=len(post),
                     bs_pre=bs(pre),bs_pre2=bs(pre2),bs_post=bs(post),notional_pre2=pre2.notional.sum(),notional_post=post.notional.sum()))
d=pd.DataFrame(rows)
d["origin"]=np.where(d.from_rank<=15,"flicker 11-15",np.where(d.from_rank<=100,"16-100","outside top100"))
print(f"regular E1 N10 events with tape in [-300,0) and (0,300]: {len(d)} of {len(ev)}")
print("\nALL: prints/300s pre2->post (median):", d.n_pre2.median(), "->", d.n_post.median(), "| buy-vol share pre->pre2->post (mean):", d[["bs_pre","bs_pre2","bs_post"]].mean().round(3).tolist(), "| $notional/300s median:", round(d.notional_pre2.median()), "->", round(d.notional_post.median()))
print("\nby ORIGIN (mean buy-share pre2 -> post; median prints; median notional):")
print(d.groupby("origin").agg(n=("bs_post","size"),bs_pre2=("bs_pre2","mean"),bs_post=("bs_post","mean"),prints_pre2=("n_pre2","median"),prints_post=("n_post","median"),usd_pre2=("notional_pre2","median"),usd_post=("notional_post","median")).round(3).to_string())
print("\nby BAND:")
print(d.groupby("band",observed=True).agg(n=("bs_post","size"),bs_pre2=("bs_pre2","mean"),bs_post=("bs_post","mean"),prints_pre2=("n_pre2","median"),prints_post=("n_post","median")).round(3).to_string())
print("\nby DWELL:")
d["dwell"]=np.where(d.stay<=2,"flicker<=25s",np.where(d.stay>=8,"stays>=100s","mid"))
print(d.groupby("dwell").agg(n=("bs_post","size"),bs_pre2=("bs_pre2","mean"),bs_post=("bs_post","mean"),prints_pre2=("n_pre2","median"),prints_post=("n_post","median")).round(3).to_string())

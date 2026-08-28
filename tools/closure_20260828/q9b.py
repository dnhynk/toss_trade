import os; os.makedirs('./out', exist_ok=True)
import numpy as np, pandas as pd
x = pd.read_parquet("./out/markout60.parquet")
m = pd.read_parquet("./out/tape_side.parquet")[["symbol","ts_ms","price_u","qty_u","snap_ms"]].drop_duplicates(["symbol","ts_ms","price_u","qty_u"])
x = x.merge(m, on=["symbol","ts_ms","price_u","qty_u"], how="left")
x["age_s"]=(x.ts_ms-x.snap_ms)/1000
x["fresh"]=pd.cut(x.age_s,[-1,2,4,10],labels=["<=2s","2-4s","4-10s"])
r=x[(x.sess=="regular")&(x.band.isin(["$0-2","$2-5"]))]
print("=== regular $0-5, tau=60: MM pnl by book age at print x side (staleness check) ===")
print(r.groupby(["fresh","side"],observed=True).mm_pnl.agg(["size","mean","median"]).round(4).to_string())
ev = pd.read_parquet("./out/e1n10_origin.parquet")
x=x.sort_values("ts_ms"); rows=[]
for t0,sym,fr in zip(ev.t0,ev.symbol,ev.from_rank):
    g=x[(x.symbol==sym)&(x.ts_ms>=t0-300000)&(x.ts_ms<=t0+300000)]
    if len(g)==0: continue
    rows.append(g.assign(win=np.where(g.ts_ms<t0,"pre[-300,0)","post(0,300]"),origin=np.where(fr<=15,"flicker","16+")))
e=pd.concat(rows)
print("\n=== regular top-10 entries: MM pnl (tau=60) by window x side [selection has lookahead; mechanism check] ===")
print(e.groupby(["win","side"]).mm_pnl.agg(["size","mean","median"]).round(4).to_string())
print("\n-- origin 16+ only:")
print(e[e.origin=="16+"].groupby(["win","side"]).mm_pnl.agg(["size","mean","median"]).round(4).to_string())

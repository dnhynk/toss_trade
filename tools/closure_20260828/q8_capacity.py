import os; os.makedirs('./out', exist_ok=True)
import numpy as np, pandas as pd
m = pd.read_parquet("./out/tape_side.parquet")
r = m[m.sess.isin(["regular","day","pre"])].copy()
r["minute"]=r.ts_ms//60000
g=r.groupby(["sess","band","symbol","minute"],observed=True)
per_min=pd.DataFrame({"usd":g.notional.sum(),"usd_at_bid":g.apply(lambda d:d.loc[d.side=="SELL@bid-","notional"].sum()),"usd_at_ask":g.apply(lambda d:d.loc[d.side=="BUY@ask+","notional"].sum())}).reset_index()
print("Per symbol-minute printed notional (tape is a 50-cap SAMPLE; true volume is higher). Exploration-B era.")
out=per_min.groupby(["sess","band"],observed=True).agg(sym_minutes=("usd","size"),usd_p50=("usd","median"),usd_p90=("usd","quantile"),bid_side_p50=("usd_at_bid","median"),frac_min_bid_ge500=("usd_at_bid",lambda s:(s>=500).mean()),frac_min_ask_ge500=("usd_at_ask",lambda s:(s>=500).mean())).round(2)
print(out.to_string())
# how many distinct symbols per regular session carried tape in $0-5
r2=r[(r.sess=="regular")&(r.band.isin(["$0-2","$2-5"]))]
r2=r2.assign(date=pd.to_datetime(r2.ts_ms,unit="ms",utc=True).dt.date)
print("\n$0-5 regular: distinct tier-3 symbols with tape per UTC date:", r2.groupby("date").symbol.nunique().to_dict())

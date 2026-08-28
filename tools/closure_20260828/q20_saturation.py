import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26"); KST=9*3600*1000
tr = pd.read_sql("SELECT symbol, ts_ms FROM trades_snap WHERE ts_ms>=? AND ts_ms<?", con, params=(A,B))
h=((tr.ts_ms+KST)%86400000)//60000
tr["sess"]=np.select([(h>=540)&(h<1020),(h>=1020)&(h<1350),(h>=1350)|(h<300)],["day","pre","regular"],"other")
tr["b4"]=tr.ts_ms//4000
c=tr.groupby(["sess","symbol","b4"]).size()
for s in ("day","pre","regular"):
    x=c.xs(s,level=0); print(f"{s:8} 4s-buckets with prints: {len(x):7}  >=40 prints: {(x>=40).mean():.3%}  >=50: {(x>=50).mean():.3%}  share of prints in >=40 buckets: {x[x>=40].sum()/x.sum():.1%}")

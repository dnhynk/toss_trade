import os; os.makedirs('./out', exist_ok=True)
import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
sh = pd.read_sql("""
SELECT t.snap_ms AS snap_ms, t.symbol AS symbol, CAST(t.vol_qu AS REAL)/m.vol_qu AS share, t.rank AS trank, m.rank AS mrank
FROM rankings_snap t JOIN rankings_snap m
  ON m.symbol=t.symbol AND m.ranking_type='MARKET_TRADING_VOLUME' AND m.duration='realtime'
  AND m.snap_ms BETWEEN t.snap_ms-8000 AND t.snap_ms+8000
WHERE t.ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND t.duration='realtime'
  AND t.snap_ms>=? AND t.snap_ms<? AND m.vol_qu>0 AND t.vol_qu>0
  AND t.amount_u <> 9223372036854775807 AND m.amount_u <> 9223372036854775807""", con, params=(A,B)).drop_duplicates(["snap_ms","symbol"]).sort_values("snap_ms")
sh.to_parquet("./out/toss_share.parquet")
x = pd.read_parquet("./out/markout60.parquet").sort_values("ts_ms")
x = pd.merge_asof(x, sh[["snap_ms","symbol","share"]].rename(columns={"snap_ms":"sh_ms"}), left_on="ts_ms", right_on="sh_ms", by="symbol", direction="backward", tolerance=120000)
print(f"fills with a Toss-share reading (<=120s old, symbol in both top-100 lists): {x.share.notna().sum()} of {len(x)}")
x=x.dropna(subset=["share"])
x["share_b"]=pd.cut(x.share,[0,0.1,0.3,0.6,1.0,100],labels=["<10%","10-30%","30-60%","60-100%",">100%"])
for s in ("regular","pre","day"):
    r=x[x.sess==s]
    print(f"\n=== {s}: MM pnl (tau=60) by Toss-share bucket x side, $0-5 only ===")
    rr=r[r.band.isin(["$0-2","$2-5"])]
    print(rr.groupby(["share_b","side"],observed=True).mm_pnl.agg(["size","mean","median"]).round(4).to_string())
# is high Toss share associated with negative drift? mid change over 60s regardless of side
x["drift60"]=np.where(x.side=="SELL@bid-", (x.mid_fwd-x.bid1_u)/x.bid1_u - x.half_spread, -((x.ask1_u-x.mid_fwd)/x.ask1_u - x.half_spread))  # approx mid(t+60)/mid(t)-1
print("\n=== regular $0-5: approx 60s mid drift after a print, by Toss-share bucket ===")
r=x[(x.sess=="regular")&(x.band.isin(["$0-2","$2-5"]))]
print(r.groupby("share_b",observed=True).drift60.agg(["size","mean","median"]).round(4).to_string())

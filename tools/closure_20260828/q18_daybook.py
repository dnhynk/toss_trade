import sqlite3, datetime as dt, json, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
ob = pd.read_sql("SELECT symbol, snap_ms, ts_ms, bid1_u, bid1_qu, ask1_u, ask1_qu, depth_json FROM orderbook_snap WHERE snap_ms>=? AND snap_ms<? AND bid1_u>0 AND ask1_u>0", con, params=(A,B))
ob["kst"]=pd.to_datetime(ob.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); hm=ob.kst.dt.hour*60+ob.kst.dt.minute
day=ob[(hm>=540)&(hm<1020)].copy()
print("day-session book snaps:", len(day), "symbols:", day.symbol.nunique(), "dates:", day.kst.dt.date.nunique())
print("sample depth_json:", day.depth_json.iloc[0][:300])
day["levels_b"]=day.depth_json.apply(lambda s: len(json.loads(s).get("bids",[])))
day["levels_a"]=day.depth_json.apply(lambda s: len(json.loads(s).get("asks",[])))
print("levels bids p50/p90:", day.levels_b.median(), day.levels_b.quantile(.9), " asks:", day.levels_a.median(), day.levels_a.quantile(.9))
day["mid"]=(day.bid1_u+day.ask1_u)/2/1e6; day["spr"]=(day.ask1_u-day.bid1_u)/1e6; day["rel"]=day.spr/day.mid
day["band"]=pd.cut(day.mid,[0,1,2,5,20,1e9],labels=["<$1","$1-2","$2-5","$5-20","$20+"])
day["tick"]=np.where(day.mid<1,0.0001,0.01); day["spr_ticks"]=(day.spr/day.tick).round()
day["bid1_usd"]=day.bid1_qu/1e6*day.bid1_u/1e6; day["ask1_usd"]=day.ask1_qu/1e6*day.ask1_u/1e6
g=day.groupby("band",observed=True)
print("\nDAY session L1 by band: snaps, rel spread p50, spread in ticks p50/p75, bid1 $ p25/p50/p75, ask1 $ p50")
print(pd.DataFrame({"snaps":g.size(),"rel_p50":g.rel.median().round(4),"ticks_p50":g.spr_ticks.median(),"ticks_p75":g.spr_ticks.quantile(.75),"bid1$_p25":g.bid1_usd.quantile(.25).round(0),"bid1$_p50":g.bid1_usd.median().round(0),"bid1$_p75":g.bid1_usd.quantile(.75).round(0),"ask1$_p50":g.ask1_usd.median().round(0)}).to_string())
# how often does the touch move between consecutive snaps (4s)?
day=day.sort_values(["symbol","snap_ms"]); day["dbid"]=day.groupby("symbol").bid1_u.diff(); day["dt"]=day.groupby("symbol").snap_ms.diff()
x=day[(day.dt<=8000)]
print("\ntouch (bid1) unchanged between consecutive snaps:", (x.dbid==0).mean().round(3), " up:", (x.dbid>0).mean().round(3), " down:", (x.dbid<0).mean().round(3))
# book ts vs our snap: age
day["age"]=(day.snap_ms-day.ts_ms)/1000
print("book age (snap - server ts) p50/p90 s:", day.age.median(), day.age.quantile(.9))
# per-symbol-day activity: snaps and names by date
print("\nsymbol-days with >=1000 snaps (>=~67 min coverage):", (day.groupby(["symbol",day.kst.dt.date]).size()>=1000).sum(), "of", day.groupby(["symbol",day.kst.dt.date]).ngroups)

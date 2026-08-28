# Is the day-session price real? candles_1m by session for names in TOSS top-20 at 16:55; and day-session tape from trades_snap
import sqlite3, datetime as dt, numpy as np, pandas as pd
DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=180); con.execute("PRAGMA query_only=ON")
def ms(s): return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
A, B = ms("2026-08-18"), ms("2026-08-26")
rk = pd.read_sql("SELECT snap_ms, symbol, last_u, vol_qu FROM rankings_snap WHERE ranking_type='TOSS_SECURITIES_TRADING_VOLUME' AND duration='realtime' AND rank<=20 AND snap_ms>=? AND snap_ms<?", con, params=(A,B))
rk["kst"]=pd.to_datetime(rk.snap_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); rk["hm"]=rk.kst.dt.hour*60+rk.kst.dt.minute
anchor=rk[(rk.hm>=16*60+50)&(rk.hm<=16*60+55)&(rk.last_u<5e6)]
syms=tuple(anchor.symbol.unique()); print("anchor names:",len(syms))
q=",".join("?"*len(syms))
c = pd.read_sql(f"SELECT symbol, ts_ms, close_u, vol_qu FROM candles_1m WHERE symbol IN ({q}) AND ts_ms>=? AND ts_ms<?", con, params=(*syms,A,B))
c["kst"]=pd.to_datetime(c.ts_ms,unit="ms",utc=True).dt.tz_convert("Asia/Seoul"); m=c.kst.dt.hour*60+c.kst.dt.minute
c["sess"]=np.select([(m>=540)&(m<1020),(m>=1020)&(m<1350),(m>=1350)|(m<300),(m>=300)&(m<530)],["day","pre","regular","after"],"closed")
print("\n1-min candles for those names, by session: bars, median bar volume (shares), total $ volume (M)")
c["usd"]=c.close_u/1e6*c.vol_qu/1e6
print(c.groupby("sess").agg(bars=("ts_ms","size"),vol_p50=("vol_qu",lambda s:(s/1e6).median()),usd_total_M=("usd",lambda s:s.sum()/1e6)).round(2).to_string())
# 12s ranking vol_qu (rolling window) for these names in day vs pre vs regular -> is the day-session tape real?
rk2=rk[rk.symbol.isin(syms)]
rk2["sess"]=np.select([(rk2.hm>=540)&(rk2.hm<1020),(rk2.hm>=1020)&(rk2.hm<1350),(rk2.hm>=1350)|(rk2.hm<300),(rk2.hm>=300)&(rk2.hm<530)],["day","pre","regular","after"],"closed")
print("\nranking rolling-window vol_qu (shares, median) for those names by session:")
print(rk2.groupby("sess").vol_qu.median().div(1e6).round(0).to_string())
# how stale is last_u in day session: fraction of consecutive snaps where last_u unchanged
rk2=rk2.sort_values(["symbol","snap_ms"]); rk2["same"]=rk2.groupby("symbol").last_u.diff().eq(0)
print("\nfraction of consecutive top-20 snaps with UNCHANGED last_u, by session (staleness proxy):")
print(rk2.groupby("sess").same.mean().round(3).to_string())

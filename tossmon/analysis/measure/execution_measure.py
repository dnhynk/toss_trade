"""Execution-reality measurement (docs/18) - conditional spread/depth and cost model.

Replaces the "average spread at any time" figure with distributions conditioned on
what we would actually trade: price band, session, order size, and proximity to a move.

Re-runs unchanged as collection days accumulate: every market day present in the
snapshot is processed and reported separately plus pooled.

Reads a SNAPSHOT copy of the live DB (the collector is running; the production file is
never locked). Live calls: 0. Console ASCII.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer")
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tossmon.analysis import execution as X  # noqa: E402

LIVE_DB = HERE / "live_snapshot.db"
OUT = HERE / "execution"
MIN_MS = 60_000

NOTIONALS = (500, 1000, 2000, 5000)
EXIT_MODES = ("cross", "passive", "mid")
#: ET offsets for session labelling come from the calendar in the backfill capture.
CAL = Path(r"C:/Users/dongh/orca/workspaces/toss_trade/w4-collector/data/backfill/"
           r"calendar_us.json")

#: B5 - drop the <=60 s contemporaneous leak by using only closes strictly before
#: the snapshot's own minute. Set False to reproduce the original docs/18 numbers.
STRICT_PRIOR = True


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def session_of(ts_ms: int, cal_by_date) -> str:
    for md in cal_by_date:
        for name in ("day", "pre", "regular", "after"):
            w = getattr(md, name, None)
            if w is not None and w.start_ms <= ts_ms < w.end_ms:
                return name
    return "unknown"


def load_books(conn, day: str) -> pd.DataFrame:
    ob = pd.read_sql_query(
        "SELECT symbol, snap_ms, bid1_u, bid1_qu, ask1_u, ask1_qu, depth_json "
        "FROM orderbook_snap WHERE date(snap_ms/1000,'unixepoch')=? "
        "ORDER BY symbol, snap_ms", conn, params=(day,))
    rows = []
    for r in ob.itertuples(index=False):
        bids, asks = X.parse_depth(r.depth_json)
        rel = X.relative_spread(r.bid1_u, r.ask1_u)
        m = X.mid_u(r.bid1_u, r.ask1_u)
        tob = X.top_of_book_usd(r.bid1_u, r.bid1_qu, r.ask1_u, r.ask1_qu)
        rec = {"symbol": r.symbol, "snap_ms": int(r.snap_ms), "rel_spread": rel,
               "mid_usd": (m / X.MICRO if m == m else float("nan")),
               "tob_min_usd": tob["min_usd"], "tob_ask_usd": tob["ask_usd"],
               "n_bid_levels": len(bids), "n_ask_levels": len(asks),
               "depth_ask_usd": X.depth_to_usd(asks),
               "depth_bid_usd": X.depth_to_usd(bids)}
        rec["price_band"] = X.price_band(rec["mid_usd"])
        for n in NOTIONALS:
            for mode in EXIT_MODES:
                rt = X.round_trip_cost(bids, asks, n, exit_mode=mode)
                rec[f"rt_{n}_{mode}"] = rt["total"]
                if mode == "cross":
                    rec[f"entry_{n}"] = rt["entry"]
                    rec[f"lv_{n}"] = rt["entry_levels"]
                    rec[f"exh_{n}"] = rt["entry_exhausted"]
        rows.append(rec)
    return pd.DataFrame(rows)


def tbl(df: pd.DataFrame, by: str, cols: dict[str, str]) -> pd.DataFrame:
    g = df.groupby(by, observed=True)
    out = pd.DataFrame({"n": g.size()})
    for label, col in cols.items():
        out[label] = g[col].median()
    return out.reset_index()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    from tossmon.analysis import baselines as B
    cal = B.load_calendar(CAL)

    conn = ro(LIVE_DB)
    try:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date(snap_ms/1000,'unixepoch') FROM orderbook_snap "
            "ORDER BY 1")]
        print(f"orderbook collection days: {len(days)} -> {days}")
        res: dict = {"days": days, "notionals_usd": list(NOTIONALS), "per_day": {}}
        allb = []
        for day in days:
            b = load_books(conn, day)
            b["day"] = day
            allb.append(b)
        books = pd.concat(allb, ignore_index=True)
        cal_day = [md for md in cal if md.date in days]
        books["session"] = [session_of(t, cal_day) for t in books["snap_ms"]]
        books.to_parquet(OUT / "books.parquet")
        valid = books[books["rel_spread"].notna()]
        print(f"\nsnapshots: {len(books)} total, {len(valid)} with a valid two-sided "
              f"book ({len(books) - len(valid)} unusable, reported not filled)")
        print(f"symbols: {books['symbol'].nunique()}")
        res["n_snaps"] = int(len(books))
        res["n_valid"] = int(len(valid))
        res["n_symbols"] = int(books["symbol"].nunique())

        # ---------------- 2. price band x session -------------------------- #
        print("\n===== relative spread by price band =====")
        t = tbl(valid, "price_band",
                {"rel_spread": "rel_spread", "tob_min_usd": "tob_min_usd",
                 "depth_ask_usd": "depth_ask_usd", "mid_usd": "mid_usd"})
        t = t.set_index("price_band").reindex(
            [b for b in X.PRICE_BAND_ORDER if b in set(t["price_band"])]).reset_index()
        print(t.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        res["by_price_band"] = t.to_dict("records")

        print("\n===== relative spread by session =====")
        ts = tbl(valid, "session",
                 {"rel_spread": "rel_spread", "tob_min_usd": "tob_min_usd",
                  "depth_ask_usd": "depth_ask_usd"})
        print(ts.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        res["by_session"] = ts.to_dict("records")

        print("\n===== price band x session (median relative spread / n) =====")
        px = valid.pivot_table(index="price_band", columns="session",
                               values="rel_spread", aggfunc="median")
        pn = valid.pivot_table(index="price_band", columns="session",
                               values="rel_spread", aggfunc="size")
        print(px.to_string(float_format=lambda x: f"{x:.4f}"))
        print("counts:")
        print(pn.to_string())
        res["band_x_session_median"] = json.loads(px.to_json())
        res["band_x_session_n"] = json.loads(pn.to_json())

        # ---------------- 3. executable size ------------------------------- #
        print("\n===== executable size: order vs top-of-book =====")
        print("top-of-book notional $ (thinner side): "
              f"{X.summarize(valid['tob_min_usd'])}")
        print("depth to 10 levels, ask side $: "
              f"{X.summarize(valid['depth_ask_usd'])}")
        size_rows = []
        for n in NOTIONALS:
            lv = valid[f"lv_{n}"]
            exh = valid[f"exh_{n}"]
            frac = (n / valid["tob_min_usd"]).replace([np.inf, -np.inf], np.nan)
            size_rows.append({
                "notional_usd": n,
                "median_levels_swept": float(lv.median()),
                "share_needing_2plus_levels": float((lv >= 2).mean()),
                "share_book_exhausted": float(exh.mean()),
                "order_as_pct_of_top_of_book_median": float(frac.median()),
                "share_order_exceeds_top_of_book": float((frac > 1).mean()),
            })
        sz = pd.DataFrame(size_rows)
        print(sz.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        res["size"] = size_rows

        # ---------------- 4. effective round-trip cost --------------------- #
        print("\n===== effective ROUND-TRIP cost by size and exit mode =====")
        cost_rows = []
        for n in NOTIONALS:
            for mode in EXIT_MODES:
                s = X.summarize(valid[f"rt_{n}_{mode}"])
                cost_rows.append({"notional_usd": n, "exit_mode": mode, **s})
        cr = pd.DataFrame(cost_rows)
        print(cr.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        res["round_trip"] = cost_rows

        print("\n===== round-trip (cross exit, $1000) by price band =====")
        rb = tbl(valid, "price_band", {"rt_cross": "rt_1000_cross",
                                       "rt_passive": "rt_1000_passive",
                                       "entry_only": "entry_1000"})
        rb = rb.set_index("price_band").reindex(
            [b for b in X.PRICE_BAND_ORDER if b in set(rb["price_band"])]).reset_index()
        print(rb.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        res["round_trip_by_band"] = rb.to_dict("records")

        # ---------------- 1. spread vs concurrent move --------------------- #
        print("\n===== does the spread widen or tighten while price moves? =====")
        syms = list(books["symbol"].unique())
        cm = pd.read_sql_query(
            "SELECT symbol, ts_ms, close_u, high_u, low_u, vol_qu FROM candles_1m "
            "WHERE symbol IN (%s) ORDER BY symbol, ts_ms" % ",".join("?" * len(syms)),
            conn, params=syms)
        # 캔들 ↔ 호가 스냅 **교차축 조인**. 캔들은 종료 시각 라벨이라 라벨 T 인 봉의
        # 내용은 분 `T//60000 - 1` 이다 (docs/12 §6.1) — 라벨을 그대로 분으로 쓰면
        # 봉이 한 분 미래의 슬롯에 붙는다. 스냅의 `snap_ms` 는 진짜 순간이라 보정 없음.
        cm["minute"] = (cm["ts_ms"] - MIN_MS) // MIN_MS
        prof = []
        for sym, g in cm.groupby("symbol"):
            g = g.sort_values("ts_ms")
            # B5: the conditioning axis is the PAST 5 bars - shift(5) looks backward.
            # No future bar is ever read (tests/test_measure_conditioning.py enforces).
            ret5 = (g["close_u"] / g["close_u"].shift(5) - 1).abs()
            vol5 = g["vol_qu"].rolling(5, min_periods=1).sum()
            if STRICT_PRIOR:
                # A snapshot inside minute m is matched to the bar COVERING minute m
                # (see the label fix above) - contemporaneous. Shifting one bar makes
                # the value depend only on bars covering up to minute m-1.
                #
                # The old comment here read "the bar that CLOSES at m+1", which assumed
                # a START-time label; under the end-time label (docs/12 6.1) the bar was
                # already one minute in the past, so this shift used to push the value
                # TWO minutes back. Fixing the join above restores it to one.
                ret5, vol5 = ret5.shift(1), vol5.shift(1)
            prof.append(pd.DataFrame({"symbol": sym, "minute": g["minute"],
                                      "abs_ret_5m": ret5, "vol5_qu": vol5}))
        pf = pd.concat(prof, ignore_index=True)
        vb = valid.copy()
        vb["minute"] = vb["snap_ms"] // MIN_MS
        j = vb.merge(pf, on=["symbol", "minute"], how="left")
        jj = j[j["abs_ret_5m"].notna() & (j["abs_ret_5m"] > 0)]
        print(f"snapshots matched to a concurrent 5-min move: {len(jj)} of {len(vb)}")
        if len(jj) >= 20:
            jj = jj.copy()
            jj["move_q"] = pd.qcut(jj["abs_ret_5m"], 4, duplicates="drop")
            mv = jj.groupby("move_q", observed=True).agg(
                n=("rel_spread", "size"), abs_ret=("abs_ret_5m", "median"),
                rel_spread=("rel_spread", "median"),
                tob_usd=("tob_min_usd", "median"),
                depth_usd=("depth_ask_usd", "median"))
            print(mv.to_string(float_format=lambda x: f"{x:.4f}"))
            rho = jj["abs_ret_5m"].rank().corr(jj["rel_spread"].rank())
            print(f"Spearman(|5m move|, relative spread) = {rho:+.3f}")
            res["spread_vs_move"] = {"n": int(len(jj)), "spearman": float(rho),
                                     "quartiles": json.loads(mv.to_json())}
        else:
            print("  too few matched snapshots - not reported")
            res["spread_vs_move"] = {"n": int(len(jj)), "note": "insufficient"}

        # ---------------- 5. breakeven vs realised peaks -------------------- #
        print("\n===== breakeven vs event peak returns =====")
        ev = pd.read_parquet(HERE / "phase1c_v2" / "ev_v3.parquet")
        peak = pd.to_numeric(ev["peak_ret"], errors="coerce").dropna()
        res["peak_ret"] = {"n": int(len(peak)), "median": float(peak.median()),
                           "p25": float(peak.quantile(.25)),
                           "p75": float(peak.quantile(.75)),
                           "p90": float(peak.quantile(.90))}
        print(f"backfill event peak_ret: median {peak.median():.4f} "
              f"p75 {peak.quantile(.75):.4f} p90 {peak.quantile(.90):.4f} "
              f"(n={len(peak)})")
        be_rows = []
        for band in [b for b in X.PRICE_BAND_ORDER if b in set(valid["price_band"])]:
            sub = valid[valid["price_band"] == band]
            for mode in ("cross", "passive"):
                be = float(sub[f"rt_1000_{mode}"].median())
                if not (be == be):
                    continue
                be_rows.append({
                    "price_band": band, "exit_mode": mode, "n": int(len(sub)),
                    "breakeven_pct": be,
                    "share_events_above_breakeven": float((peak > be).mean()),
                })
        be = pd.DataFrame(be_rows)
        print(be.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        res["breakeven"] = be_rows
    finally:
        conn.close()

    (OUT / "execution.json").write_text(json.dumps(res, indent=1, default=str),
                                        encoding="utf-8")
    print(f"\nwrote {OUT / 'execution.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

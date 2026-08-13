"""D-21 - what a ranking-driven tier3 lane would cost and how it would behave.

Read-only. Opens the collector DB with `mode=ro` and reads `collector.log` as
text. **No live API call is made from this file.** ASCII output only (cp949).

Why this file exists
--------------------
`docs/59` measured that 71-99% of the symbols that reach a ranking top-100 have
no tape of ours at all. `docs/61` asks the next question: if ranking entry became
a promotion reason, what would it cost and would it oscillate. Both answers have
to come from data we already hold, because the change is irreversible (the ticks
we do not take today cannot be fetched later) and because the same knob collapsed
collection once already (2026-08-04, `STRATEGY-VERDICTS` 4.4-E).

Six measurements, each printed with its own denominator:

  [1] where the tape actually comes from      - `promotions` x `trades_snap`
  [2] what the current ranking path reaches   - reason='ranking_entry'
  [3] the arrival process at the top of each ranking list
  [4] lane simulation: K reserved tier3 slots, dwell D, replayed on rankings_snap
  [5] request cost of K extra tier3 members, against both budget ceilings
  [6] disk cost: bytes/row weighed on a replica, rows/member-day from live data

Run:
  python tools/ranking_promotion_sim.py --db <db> --log <log> --out <json>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter, defaultdict

# US eastern offset. August 2026 is entirely EDT. Hard-coded on purpose - a tz
# library would let this drift silently later (same choice as tools/poll5s_cost.py).
ET_OFFSET_H = -4
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

#: Ranking types worth asking about. The two _AMOUNT lists stopped arriving on
#: 2026-08-04 (docs/INDEX correction 1) so they are listed but will read as stale.
TYPES = ("TOP_GAINERS", "TOSS_SECURITIES_TRADING_VOLUME", "MARKET_TRADING_VOLUME")

#: Live collection shape as of 2026-08-13, from `w5-ops/config/config.yaml` and
#: confirmed by the telemetry `config_sig`. These are inputs, not measurements.
LIVE = {
    "limit_market_data": 10.0,
    "usage_ratio": 0.85,
    "tier1_max": 1500,
    "tier2_max": 300,
    "tier3_max": 10,
    "tier1_sweep_s": 45.0,
    "tier3_trades_s": 3.0,
    "tier3_orderbook_s": 4.0,
    "batch_max": 200,
}
#: budget.py HEADROOM / PLAN_RESERVE_FRAC. Kept as literals so this tool does not
#: import the collector - if they drift, the test in tests/ that compares them fails.
HEADROOM = 0.95
PLAN_RESERVE_FRAC = 0.10

TRADES_DDL = ("CREATE TABLE trades_snap (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,"
              " price_u INTEGER NOT NULL, qty_u INTEGER NOT NULL,"
              " PRIMARY KEY (symbol, ts_ms, price_u, qty_u)) WITHOUT ROWID")
TRADES_IDX = ("CREATE INDEX ix_trades_ts ON trades_snap (ts_ms)",)
BOOK_DDL = ("CREATE TABLE orderbook_snap (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " symbol TEXT NOT NULL, snap_ms INTEGER NOT NULL, ts_ms INTEGER,"
            " bid1_u INTEGER, bid1_qu INTEGER, ask1_u INTEGER, ask1_qu INTEGER,"
            " depth_json TEXT NOT NULL, spread_u INTEGER, imbalance_signed REAL)")
BOOK_IDX = ("CREATE INDEX ix_ob_symbol_ms ON orderbook_snap (symbol, snap_ms)",)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def pct(values, q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return float(s[min(len(s) - 1, max(0, int(math.ceil(q * len(s))) - 1))])


def is_regular(ms: int) -> bool:
    """US regular session, 09:30-16:00 ET."""
    m = (ms + ET_OFFSET_H * HOUR_MS) % DAY_MS // 60_000
    return 9 * 60 + 30 <= m < 16 * 60


def utc_day(ms: int) -> int:
    return ms // DAY_MS


# --------------------------------------------------------------------------- #
# [1] where does the tape come from
# --------------------------------------------------------------------------- #
def measure_tape_origin(db: str) -> dict:
    """Which tier actually writes `trades_snap`.

    Code says tier3 only (`loops._poll_trades` is called from `run_tier3_micro`
    and nowhere else). This checks the claim against the data instead of trusting
    the reading: the set of symbols that ever reached tier3 and the set of symbols
    with any trade row should coincide.
    """
    cx = ro(db)
    c = cx.cursor()
    tier3 = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM promotions WHERE to_tier=3")}
    taped = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM trades_snap")}
    by = [(int(t), str(r), int(n)) for t, r, n in c.execute(
        "SELECT to_tier, reason, count(*) FROM promotions GROUP BY 1,2 ORDER BY 1,3 DESC")]
    span = c.execute("SELECT min(ts_ms), max(ts_ms) FROM promotions").fetchone()
    cx.close()
    return {
        "promotions_by_tier_reason": by,
        "promotions_span_ms": [int(span[0]), int(span[1])],
        "tier3_symbols": len(tier3),
        "taped_symbols": len(taped),
        "taped_not_tier3": sorted(taped - tier3)[:20],
        "tier3_not_taped": sorted(tier3 - taped)[:20],
        "sets_identical": tier3 == taped,
        "tier3_entry_reasons": sorted(
            {r for t, r, _ in by if t == 3}),
    }


# --------------------------------------------------------------------------- #
# [2] what the current ranking path reaches
# --------------------------------------------------------------------------- #
def measure_current_ranking_path(db: str) -> dict:
    cx = ro(db)
    c = cx.cursor()
    tot, syms = c.execute(
        "SELECT count(*), count(DISTINCT symbol) FROM promotions "
        "WHERE reason='ranking_entry'").fetchone()
    reached = c.execute(
        "SELECT count(DISTINCT r.symbol) FROM promotions r WHERE r.reason='ranking_entry'"
        " AND EXISTS (SELECT 1 FROM promotions p WHERE p.symbol=r.symbol"
        "             AND p.to_tier=3 AND p.ts_ms>=r.ts_ms)").fetchone()[0]
    per_day = [(str(d), int(n), int(s)) for d, n, s in c.execute(
        "SELECT date(ts_ms/1000,'unixepoch'), count(*), count(DISTINCT symbol)"
        " FROM promotions WHERE reason='ranking_entry' GROUP BY 1 ORDER BY 1")]
    tiers = [(int(t), int(n)) for t, n in c.execute(
        "SELECT to_tier, count(*) FROM promotions WHERE reason='ranking_entry'"
        " GROUP BY 1 ORDER BY 1")]
    cx.close()
    return {"promotions": int(tot), "distinct_symbols": int(syms),
            "later_reached_tier3": int(reached), "to_tier": tiers, "per_day": per_day}


# --------------------------------------------------------------------------- #
# [3] the arrival process at the top of each list
# --------------------------------------------------------------------------- #
def measure_arrivals(db: str, types, top_ns, regular_only: bool) -> dict:
    """How many distinct symbols show up in the top N of each list, and how often.

    This bounds every design: a lane cannot promote more symbols than arrive.
    """
    cx = ro(db)
    c = cx.cursor()
    out = {}
    for rtype in types:
        for n in top_ns:
            rows = c.execute(
                "SELECT snap_ms, symbol FROM rankings_snap"
                " WHERE ranking_type=? AND rank<=? ORDER BY snap_ms", (rtype, n)).fetchall()
            if regular_only:
                rows = [r for r in rows if is_regular(int(r[0]))]
            if not rows:
                out[f"{rtype}@{n}"] = {"snapshots": 0}
                continue
            per_day_syms = defaultdict(set)
            seen: set[str] = set()
            first_seen_day = Counter()
            snaps = set()
            for ms, sym in rows:
                d = utc_day(int(ms))
                per_day_syms[d].add(sym)
                snaps.add(int(ms))
                if sym not in seen:
                    seen.add(sym)
                    first_seen_day[d] += 1
            days = sorted(per_day_syms)
            out[f"{rtype}@{n}"] = {
                "snapshots": len(snaps),
                "rows": len(rows),
                "days": len(days),
                "distinct_symbols_total": len(seen),
                "distinct_per_day_mean": round(
                    statistics.mean(len(per_day_syms[d]) for d in days), 1),
                "distinct_per_day_max": max(len(per_day_syms[d]) for d in days),
                "new_symbols_per_day": [int(first_seen_day[d]) for d in days],
                "new_symbols_per_day_mean": round(
                    statistics.mean(first_seen_day[d] for d in days), 1),
            }
    cx.close()
    return out


# --------------------------------------------------------------------------- #
# [4] the lane simulation
# --------------------------------------------------------------------------- #
def simulate_lane(db: str, rtype: str, top_n: int, slots: int, dwell_s: float,
                  regular_only: bool, cooldown_s: float = 600.0,
                  policy: str = "sticky") -> dict:
    """Replay `rankings_snap` through a reserved tier3 lane of `slots` seats.

    Two seat policies, because they answer different questions and the numbers
    are not close:

    * **sticky** - a symbol inside the top `top_n` claims a free seat and keeps
      it while it stays there; the seat is released `dwell_s` after the symbol
      was last seen in the top N. Maximises *depth* on whoever is at the head.
    * **rotate** - a seat is held for exactly `dwell_s` and then released, and
      the symbol goes on `cooldown_s`. The next admission is the best-ranked
      symbol that is neither seated nor cooling. Maximises *breadth*.

    This is the ranking-driven part only. It cannot model the score channel -
    there is no offline candle score - so it answers "how does the lane behave",
    not "what does the whole tier machine do". Section 8 of docs/61 says so.

    Success criterion is the one 2026-08-04 forced on us
    (`STRATEGY-VERDICTS` 4.4-E): **zero oscillation AND new promotions > 0**.
    A lane that never turns over is a failure, not a stable system.
    """
    cx = ro(db)
    rows = cx.execute(
        "SELECT snap_ms, rank, symbol FROM rankings_snap"
        " WHERE ranking_type=? AND rank<=? ORDER BY snap_ms, rank",
        (rtype, top_n)).fetchall()
    cx.close()
    if regular_only:
        rows = [r for r in rows if is_regular(int(r[0]))]
    if not rows:
        return {"snapshots": 0}

    by_snap: "dict[int, list[str]] " = defaultdict(list)
    for ms, _rank, sym in rows:
        by_snap[int(ms)].append(str(sym))       # already ordered by rank
    snaps = sorted(by_snap)

    dwell_ms = int(dwell_s * 1000)
    cool_ms = int(cooldown_s * 1000)
    hold: dict[str, int] = {}          # symbol -> seat expires at
    last_demote: dict[str, int] = {}
    promotions: list[tuple[int, str]] = []
    demotions = 0
    blocked = 0                         # wanted a seat, lane was full
    cooldown_blocked = 0
    occupancy: list[int] = []
    per_min = Counter()
    regaps: list[int] = []              # demote -> re-promote gap, ms
    promoted_count = Counter()

    for ms in snaps:
        for sym in [s for s, until in hold.items() if until <= ms]:
            hold.pop(sym, None)
            last_demote[sym] = ms
            demotions += 1
        for sym in by_snap[ms]:
            if sym in hold:
                if policy == "sticky":
                    hold[sym] = ms + dwell_ms   # still at the head - keep the seat
                continue                        # rotate: the clock does not restart
            if len(hold) >= slots:
                blocked += 1
                continue
            prev = last_demote.get(sym)
            if prev is not None and ms - prev < cool_ms:
                cooldown_blocked += 1
                continue
            hold[sym] = ms + dwell_ms
            promotions.append((ms, sym))
            promoted_count[sym] += 1
            per_min[ms // 60_000] += 1
            if prev is not None:
                regaps.append(ms - prev)
        occupancy.append(len(hold))

    span_min = max(1.0, (snaps[-1] - snaps[0]) / 60_000.0)
    days = sorted({utc_day(m) for m in snaps})
    per_day = Counter(utc_day(ms) for ms, _ in promotions)
    per_day_syms = defaultdict(set)
    for ms, sym in promotions:
        per_day_syms[utc_day(ms)].add(sym)
    # what fraction of the symbols that showed up in the top N ever got a seat -
    # this is the number docs/59 cares about (coverage), not the promotion count.
    arrived = {s for syms in by_snap.values() for s in syms}
    minutes_with = len(per_min)
    return {
        "policy": policy,
        "arrived_symbols": len(arrived),
        "covered_symbols": len(promoted_count),
        "coverage_pct": round(100.0 * len(promoted_count) / max(1, len(arrived)), 1),
        "distinct_per_day_mean": round(
            statistics.mean(len(per_day_syms.get(d, ())) for d in days), 1),
        "snapshots": len(snaps),
        "days": len(days),
        "span_minutes": round(span_min, 1),
        "promotions": len(promotions),
        "distinct_promoted": len(promoted_count),
        "demotions": demotions,
        "blocked_snapshot_rows": blocked,
        "cooldown_blocked": cooldown_blocked,
        "promotions_per_day": [int(per_day.get(d, 0)) for d in days],
        "promotions_per_day_min": min(int(per_day.get(d, 0)) for d in days),
        "promotions_per_minute_mean": round(len(promotions) / span_min, 4),
        "promotions_per_minute_max": max(per_min.values()) if per_min else 0,
        "minutes_with_a_promotion_pct": round(100.0 * minutes_with / span_min, 2),
        "occupancy_min": min(occupancy),
        "occupancy_p50": pct(occupancy, 0.50),
        "occupancy_p95": pct(occupancy, 0.95),
        "occupancy_max": max(occupancy),
        "empty_snapshot_pct": round(
            100.0 * sum(1 for o in occupancy if o == 0) / len(occupancy), 2),
        "full_snapshot_pct": round(
            100.0 * sum(1 for o in occupancy if o >= slots) / len(occupancy), 2),
        # oscillation: the same symbol taking a seat again after losing one.
        "repromotions": len(regaps),
        "repromotions_under_cooldown": sum(1 for g in regaps if g < cool_ms),
        "repromotion_gap_p50_s": round(pct(regaps, 0.50) / 1000.0, 1) if regaps else None,
        "repromotion_gap_min_s": round(min(regaps) / 1000.0, 1) if regaps else None,
        "symbols_promoted_more_than_once": sum(1 for v in promoted_count.values() if v > 1),
        "max_promotions_of_one_symbol": max(promoted_count.values()) if promoted_count else 0,
        # 4.4-E success criterion, evaluated here so nobody has to re-derive it
        "ok_no_oscillation": sum(1 for g in regaps if g < cool_ms) == 0,
        "ok_keeps_promoting": min(int(per_day.get(d, 0)) for d in days) > 0,
    }


# --------------------------------------------------------------------------- #
# [5] request cost
# --------------------------------------------------------------------------- #
def market_data_plan(tier3_symbols: int) -> float:
    batches = math.ceil(LIVE["tier1_max"] / LIVE["batch_max"])
    return (batches / LIVE["tier1_sweep_s"]
            + tier3_symbols / LIVE["tier3_trades_s"]
            + tier3_symbols / LIVE["tier3_orderbook_s"])


def measure_request_cost(extra_slots) -> dict:
    budget = LIVE["limit_market_data"] * LIVE["usage_ratio"]
    plan_ceiling = budget * (HEADROOM - PLAN_RESERVE_FRAC)
    shrink_ceiling = budget * HEADROOM
    per_symbol = 1.0 / LIVE["tier3_trades_s"] + 1.0 / LIVE["tier3_orderbook_s"]
    base = market_data_plan(LIVE["tier3_max"])
    rows = []
    for k in extra_slots:
        rate = market_data_plan(LIVE["tier3_max"] + k)
        rows.append({
            "extra_slots": k,
            "tier3_total": LIVE["tier3_max"] + k,
            "market_data_req_s": round(rate, 4),
            "delta_req_s": round(rate - base, 4),
            "pct_of_limit": round(100.0 * rate / LIVE["limit_market_data"], 2),
            "pct_of_plan_ceiling": round(100.0 * rate / plan_ceiling, 2),
            "under_plan_ceiling": rate <= plan_ceiling,
            "under_shrink_ceiling": rate <= shrink_ceiling,
        })
    headroom = plan_ceiling - base
    return {
        "budget_req_s": round(budget, 4),
        "plan_ceiling": round(plan_ceiling, 4),
        "shrink_ceiling": round(shrink_ceiling, 4),
        "base_plan_req_s": round(base, 4),
        "per_symbol_req_s": round(per_symbol, 4),
        "headroom_req_s": round(headroom, 4),
        "max_extra_slots_at_current_periods": int(headroom // per_symbol),
        "rows": rows,
    }


def measure_peak(log: str, since_ts: str) -> dict:
    """Observed md_peak_1s, so the request table is not read as a prediction.

    Only lines at or after `since_ts` are used: the send-time accounting fix
    (docs/52, 2026-08-12) changed what `md_peak_1s` measures, and mixing the two
    windows resurrects the inflated peak that fix removed.
    """
    peaks, limpk, srv = [], [], []
    tier3, tier3cap = [], []
    used = 0
    with open(log, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if " telemetry session=" not in line or line[:19] < since_ts:
                continue
            used += 1
            for key, sink in (("md_peak_1s=", peaks), ("md_limiter_peak=", limpk),
                              ("md_srv_s=", srv), ("tier3=", tier3),
                              ("tier3_cap=", tier3cap)):
                i = line.find(" " + key)
                if i < 0:
                    continue
                try:
                    sink.append(float(line[i + 1 + len(key):].split()[0]))
                except ValueError:
                    pass
    def sm(v):
        return {"n": len(v), "mean": round(statistics.mean(v), 2) if v else None,
                "p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": max(v) if v else None}
    # ★ the cap is not the configured maximum. The budget guard cuts it, and if it
    # is already cutting then nominal extra seats are the first thing it removes.
    cut = sum(1 for v in tier3cap if v < LIVE["tier3_max"])
    return {"lines": used, "since": since_ts, "md_peak_1s": sm(peaks),
            "md_limiter_peak": sm([v for v in limpk if v >= 0]),
            "tier3_members": sm(tier3), "tier3_cap": sm(tier3cap),
            "md_peak_at_limit_pct": round(
                100.0 * sum(1 for v in peaks if v >= LIVE["limit_market_data"])
                / max(1, len(peaks)), 1),
            "tier3_cap_below_max_pct": round(100.0 * cut / max(1, len(tier3cap)), 1),
            "tier3_cap_full_pct": round(
                100.0 * (len(tier3cap) - cut) / max(1, len(tier3cap)), 1)}


# --------------------------------------------------------------------------- #
# [6] disk cost
# --------------------------------------------------------------------------- #
def _weigh(ddl: str, indexes, insert: str, rows) -> tuple[int, int]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    rx = sqlite3.connect(path)
    rx.execute("PRAGMA page_size=4096")
    rx.execute("PRAGMA journal_mode=OFF")
    rx.execute(ddl)
    for one in indexes:
        rx.execute(one)
    rx.executemany(insert, rows)
    rx.commit()
    pc = int(rx.execute("PRAGMA page_count").fetchone()[0])
    ps = int(rx.execute("PRAGMA page_size").fetchone()[0])
    rx.close()
    os.unlink(path)
    return pc, ps


def measure_disk(db: str, sample: int, days: float, tier3_members: float) -> dict:
    """Bytes per row weighed on a replica, rows per member-day from live data.

    Same method as `tools/poll5s_cost.py` (docs/60 4-1): `length()` would miss the
    B-tree and the indexes, so real rows are pushed into an empty clone with the
    same DDL and the page count is read.
    """
    cx = ro(db)
    c = cx.cursor()
    now = int(c.execute("SELECT max(snap_ms) FROM rankings_snap").fetchone()[0])
    since = now - int(days * DAY_MS)

    trades = c.execute(
        "SELECT symbol, ts_ms, price_u, qty_u FROM trades_snap"
        " ORDER BY ts_ms DESC LIMIT ?", (sample,)).fetchall()
    book = c.execute(
        "SELECT symbol, snap_ms, ts_ms, bid1_u, bid1_qu, ask1_u, ask1_qu, depth_json,"
        " spread_u, imbalance_signed FROM orderbook_snap ORDER BY id DESC LIMIT ?",
        (sample,)).fetchall()
    trades_rows_win = int(c.execute(
        "SELECT count(*) FROM trades_snap WHERE ts_ms>=?", (since,)).fetchone()[0])
    book_rows_win = int(c.execute(
        "SELECT count(*) FROM orderbook_snap WHERE snap_ms>=?", (since,)).fetchone()[0])
    trades_total = int(c.execute("SELECT count(*) FROM trades_snap").fetchone()[0])
    book_total = int(c.execute("SELECT count(*) FROM orderbook_snap").fetchone()[0])
    page_count = int(c.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(c.execute("PRAGMA page_size").fetchone()[0])
    cx.close()

    pc_t, ps = _weigh(TRADES_DDL, TRADES_IDX,
                      "INSERT OR IGNORE INTO trades_snap VALUES (?,?,?,?)", trades)
    n_t = len({(r[0], r[1], r[2], r[3]) for r in trades})
    pc_b, _ = _weigh(BOOK_DDL, BOOK_IDX,
                     "INSERT INTO orderbook_snap (symbol, snap_ms, ts_ms, bid1_u, bid1_qu,"
                     " ask1_u, ask1_qu, depth_json, spread_u, imbalance_signed)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)", book)
    n_b = len(book)

    bpr_t = pc_t * ps / max(1, n_t)
    bpr_b = pc_b * ps / max(1, n_b)
    t_per_day = trades_rows_win / days
    b_per_day = book_rows_win / days
    free = shutil.disk_usage("C:\\").free if os.name == "nt" else shutil.disk_usage("/").free
    return {
        "window_days": days,
        "tier3_members_mean": round(tier3_members, 2),
        "trades_sample_rows": n_t, "orderbook_sample_rows": n_b,
        "trades_bytes_per_row": round(bpr_t, 2),
        "orderbook_bytes_per_row": round(bpr_b, 2),
        "trades_rows_total": trades_total, "orderbook_rows_total": book_total,
        "trades_rows_per_day": round(t_per_day, 1),
        "orderbook_rows_per_day": round(b_per_day, 1),
        "trades_rows_per_member_day": round(t_per_day / tier3_members, 1),
        "orderbook_rows_per_member_day": round(b_per_day / tier3_members, 1),
        "gib_per_member_day": round(
            (t_per_day / tier3_members * bpr_t + b_per_day / tier3_members * bpr_b)
            / 1024 ** 3, 5),
        "db_bytes": page_count * page_size,
        "disk_free_bytes": free,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
def render(res: dict) -> None:
    w = sys.stdout.write
    w("\n=== [1] where the tape comes from ===\n")
    o = res["tape_origin"]
    w("promotions rows by (to_tier, reason):\n")
    for t, r, n in o["promotions_by_tier_reason"]:
        w("  tier%d  %-16s %7d\n" % (t, r, n))
    w("tier3 entry reasons        : %s\n" % ", ".join(o["tier3_entry_reasons"]))
    w("distinct symbols ever tier3: %d\n" % o["tier3_symbols"])
    w("distinct symbols with trade: %d\n" % o["taped_symbols"])
    w("the two sets are identical : %s\n" % o["sets_identical"])

    w("\n=== [2] what the current ranking path reaches ===\n")
    p = res["current_path"]
    w("reason='ranking_entry' rows: %d  distinct symbols: %d\n"
      % (p["promotions"], p["distinct_symbols"]))
    w("  to_tier                  : %s\n" % p["to_tier"])
    w("  of those symbols, later reached tier3: %d\n" % p["later_reached_tier3"])
    w("  per UTC day (rows, distinct):\n")
    for d, n, s in p["per_day"]:
        w("    %s  %4d  %3d\n" % (d, n, s))

    w("\n=== [3] arrivals at the top of each list (regular session only) ===\n")
    w("%-38s %8s %7s %8s %9s %9s\n"
      % ("type@topN", "snaps", "days", "distinct", "sym/day", "new/day"))
    for k, v in res["arrivals"].items():
        if not v.get("snapshots"):
            w("%-38s  (no rows)\n" % k)
            continue
        w("%-38s %8d %7d %8d %9.1f %9.1f\n"
          % (k, v["snapshots"], v["days"], v["distinct_symbols_total"],
             v["distinct_per_day_mean"], v["new_symbols_per_day_mean"]))

    w("\n=== [4] lane simulation (K seats, dwell D, regular session) ===\n")
    w("coverage = distinct symbols that got a seat / distinct symbols seen in the top N\n")
    w("%-30s %6s %6s %7s %7s %7s %7s %6s %6s\n"
      % ("plan", "promo", "/day", "seen", "seated", "cover%", "reprom", "osc", "full%"))
    for name, s in res["sim"].items():
        if not s.get("snapshots"):
            w("%-30s (no rows)\n" % name)
            continue
        w("%-30s %6d %6.1f %7d %7d %7.1f %7d %6d %6.1f\n"
          % (name, s["promotions"], s["promotions"] / max(1, s["days"]),
             s["arrived_symbols"], s["covered_symbols"], s["coverage_pct"],
             s["repromotions"], s["repromotions_under_cooldown"],
             s["full_snapshot_pct"]))
    w("\n  4.4-E gate  (no oscillation AND keeps promoting):\n")
    bad = [n for n, s in res["sim"].items()
           if s.get("snapshots") and not (s["ok_no_oscillation"] and s["ok_keeps_promoting"])]
    w("    plans failing the gate: %d of %d  %s\n"
      % (len(bad), len(res["sim"]), bad if bad else ""))
    w("    min promotions/day over every plan and every session: %d\n"
      % min(s["promotions_per_day_min"] for s in res["sim"].values() if s.get("snapshots")))
    w("    worst re-promotion gap seen: %s s (cooldown is 600 s)\n"
      % min([s["repromotion_gap_min_s"] for s in res["sim"].values()
             if s.get("snapshots") and s["repromotion_gap_min_s"] is not None] or ["n/a"]))

    w("\n=== [5] request cost (MARKET_DATA) ===\n")
    r = res["request_cost"]
    w("budget %.3f  plan_ceiling %.3f  shrink_ceiling %.3f\n"
      % (r["budget_req_s"], r["plan_ceiling"], r["shrink_ceiling"]))
    w("base plan (tier3=%d) %.3f req/s   per extra symbol %.4f   headroom %.3f\n"
      % (LIVE["tier3_max"], r["base_plan_req_s"], r["per_symbol_req_s"],
         r["headroom_req_s"]))
    w("max extra tier3 slots at current periods: %d\n"
      % r["max_extra_slots_at_current_periods"])
    w("%6s %8s %10s %10s %10s %8s\n"
      % ("+K", "tier3", "req/s", "d req/s", "%plan_ceil", "under?"))
    for row in r["rows"]:
        w("%6d %8d %10.4f %10.4f %10.2f %8s\n"
          % (row["extra_slots"], row["tier3_total"], row["market_data_req_s"],
             row["delta_req_s"], row["pct_of_plan_ceiling"], row["under_plan_ceiling"]))
    pk = res["peak"]
    w("observed since %s (%d telemetry lines):\n" % (pk["since"], pk["lines"]))
    for key in ("md_peak_1s", "md_limiter_peak", "tier3_members", "tier3_cap"):
        v = pk[key]
        w("  %-16s n=%-4d mean=%-6s p50=%-5s p95=%-5s max=%s\n"
          % (key, v["n"], v["mean"], v["p50"], v["p95"], v["max"]))
    w("  md_peak_1s at the limit (%d) in %.1f%% of samples\n"
      % (LIVE["limit_market_data"], pk["md_peak_at_limit_pct"]))
    w("  tier3_cap already BELOW the configured max (%d) in %.1f%% of samples\n"
      % (LIVE["tier3_max"], pk["tier3_cap_below_max_pct"]))

    w("\n=== [6] disk cost ===\n")
    d = res["disk"]
    w("bytes/row (replica weigh)  trades_snap %.2f   orderbook_snap %.2f\n"
      % (d["trades_bytes_per_row"], d["orderbook_bytes_per_row"]))
    w("rows/day (last %.0f d)      trades %.0f   orderbook %.0f\n"
      % (d["window_days"], d["trades_rows_per_day"], d["orderbook_rows_per_day"]))
    w("tier3 members (telemetry)  %.2f\n" % d["tier3_members_mean"])
    w("rows per member-day        trades %.0f   orderbook %.0f\n"
      % (d["trades_rows_per_member_day"], d["orderbook_rows_per_member_day"]))
    w("GiB per member-day         %.5f\n" % d["gib_per_member_day"])
    w("DB now %.2f GiB   C: free %.2f GB\n"
      % (d["db_bytes"] / 1024 ** 3, d["disk_free_bytes"] / 1e9))
    for row in res["disk_plans"]:
        w("  +%d slots -> +%.4f GiB/day  (%.1f%% of the 0.398 GB/day baseline)\n"
          % (row["extra_slots"], row["gib_per_day"], row["pct_of_db_growth"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--sample", type=int, default=300_000)
    ap.add_argument("--growth-days", type=float, default=3.0)
    ap.add_argument("--since", default="2026-08-12 22:30:00",
                    help="telemetry cut - docs/52 send-time fix boundary")
    ap.add_argument("--slots", default="1,2,3")
    ap.add_argument("--dwell", default="300,900")
    ap.add_argument("--top", default="10,20")
    args = ap.parse_args()

    slots = [int(x) for x in args.slots.split(",") if x]
    dwells = [float(x) for x in args.dwell.split(",") if x]
    tops = [int(x) for x in args.top.split(",") if x]

    res: dict = {}
    res["tape_origin"] = measure_tape_origin(args.db)
    res["current_path"] = measure_current_ranking_path(args.db)
    res["arrivals"] = measure_arrivals(args.db, TYPES, tops, regular_only=True)
    res["peak"] = measure_peak(args.log, args.since)

    res["sim"] = {}
    for pol in ("sticky", "rotate"):
        for rtype in TYPES:
            for n in tops:
                for k in slots:
                    for d in dwells:
                        name = "%-6s %s@%d K%d D%d" % (
                            pol, rtype.replace("TOSS_SECURITIES_TRADING_", "T")
                            .replace("MARKET_TRADING_", "M"), n, k, int(d))
                        s = simulate_lane(args.db, rtype, n, k, d,
                                          regular_only=True, policy=pol)
                        s["_slots"], s["_dwell_s"] = k, int(d)
                        res["sim"][name] = s

    res["request_cost"] = measure_request_cost(slots + [max(slots) + 1])

    members = res["peak"]["tier3_members"]["mean"] or float(LIVE["tier3_max"])
    res["disk"] = measure_disk(args.db, args.sample, args.growth_days, members)
    per = res["disk"]["gib_per_member_day"]
    res["disk_plans"] = [
        {"extra_slots": k, "gib_per_day": round(per * k, 5),
         "pct_of_db_growth": round(100.0 * per * k * 1024 ** 3 / 0.398e9, 1)}
        for k in slots + [max(slots) + 1]]

    render(res)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
        print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

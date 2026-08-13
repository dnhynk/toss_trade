"""What does `polling.ranking_snap_s = 12 -> 5` actually cost? (docs/60)

Answers four questions with measurements, not with the "about 2.5x" that has been
carried around in prose:

  [1] cadence  - how often do we ACTUALLY poll each ranking type, split by session
  [2] cycle    - how long does one ranking cycle body take (the `_loop` sleeps
                 AFTER the body, so the real period is `configured + body`)
  [3] budget   - RANKING req/s now and at 5s, against the ceiling and against the
                 other groups; plus what the collector log says it already saw
  [4] disk     - bytes per `rankings_snap` row, measured by replaying real rows
                 into a throwaway replica with the same schema and indexes

Read-only. Opens the live DB with `mode=ro`, reads `collector.log` as text, makes
**zero** API calls and never touches the running collector.

Console output is ASCII-only on purpose: this repo has killed a supervisor with a
cp949 `UnicodeEncodeError` before.

Usage:
    python tools/poll5s_cost.py \
        --db  C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db \
        --log C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/collector.log \
        --out data/poll5s_cost.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: US Eastern offset for the measured window. August 2026 is entirely EDT
#: (DST ends 2026-11-01), so a fixed -4h is exact here and wrong outside it.
#: Stated rather than computed on purpose - a silent tz library swap would move
#: every session boundary in this report without leaving a trace.
ET_OFFSET_H = -4.0
#: US regular session in ET minutes-from-midnight: 09:30 - 16:00.
REGULAR_ET_MIN = (9 * 60 + 30, 16 * 60)

#: What the collector fires per ranking cycle - one request per type.
#: `RANKING_TYPES` in `tossmon/collector/loops.py:109`.
TYPES_PER_CYCLE = 3
#: Rows per request (`RANKING_COUNT`, loops.py:127).
ROWS_PER_REQUEST = 100
#: `limits.RANKING` in config, and `usage_ratio`.
RANKING_CEILING = 5.0
USAGE_RATIO = 0.85

RANKINGS_DDL = """
CREATE TABLE rankings_snap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_ms INTEGER NOT NULL,
    ranking_type TEXT NOT NULL,
    duration TEXT NOT NULL,
    rank INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    last_u INTEGER NOT NULL,
    vol_qu INTEGER NOT NULL,
    amount_u INTEGER NOT NULL,
    UNIQUE (snap_ms, ranking_type, duration, rank)
)
"""
RANKINGS_INDEXES = (
    "CREATE INDEX ix_rankings_symbol_ms ON rankings_snap (symbol, snap_ms)",
    "CREATE INDEX ix_rankings_type_ms ON rankings_snap (ranking_type, snap_ms)",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def ro(path: str) -> sqlite3.Connection:
    """Read-only connection. `mode=ro` is a hard requirement in this repo."""
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True,
                           timeout=60.0)


def pct(values, q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return float(s[i])


def et_minute(ms: int) -> int:
    """Minutes-from-midnight in US Eastern for an epoch-ms stamp."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) + timedelta(hours=ET_OFFSET_H)
    return dt.hour * 60 + dt.minute


def is_regular(ms: int) -> bool:
    m = et_minute(ms)
    return REGULAR_ET_MIN[0] <= m < REGULAR_ET_MIN[1]


def kst(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) + timedelta(hours=9)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def summarize(gaps, cap_s: float = 120.0) -> dict:
    """Gap stats. Gaps above `cap_s` are collector downtime, not cadence -
    they are counted separately instead of dragging the mean around."""
    inside = [g for g in gaps if g <= cap_s]
    outside = [g for g in gaps if g > cap_s]
    return {
        "n": len(gaps),
        "n_within_cap": len(inside),
        "n_gaps_over_cap": len(outside),
        "p50": round(pct(inside, 0.50), 3),
        "p90": round(pct(inside, 0.90), 3),
        "p99": round(pct(inside, 0.99), 3),
        "mean": round(statistics.fmean(inside), 3) if inside else float("nan"),
        "min": round(min(inside), 3) if inside else float("nan"),
        "max": round(max(inside), 3) if inside else float("nan"),
    }


# --------------------------------------------------------------------------- #
# [1] + [2] cadence
# --------------------------------------------------------------------------- #
def measure_cadence(db: str, days: float) -> dict:
    cx = ro(db)
    c = cx.cursor()
    now_ms = int(c.execute("SELECT max(snap_ms) FROM rankings_snap").fetchone()[0])
    since = now_ms - int(days * 86400_000)

    out: dict = {"window_days": days, "window_from_kst": kst(since),
                 "window_to_kst": kst(now_ms), "types": {}}

    rows = c.execute(
        "SELECT ranking_type, duration, snap_ms, count(*) "
        "FROM rankings_snap WHERE snap_ms >= ? "
        "GROUP BY ranking_type, duration, snap_ms ORDER BY ranking_type, snap_ms",
        (since,)).fetchall()

    per_type: dict[str, list[tuple[int, int]]] = {}
    for rtype, duration, snap_ms, n in rows:
        per_type.setdefault(f"{rtype}/{duration}", []).append((int(snap_ms), int(n)))

    for key, snaps in sorted(per_type.items()):
        reg_gaps, off_gaps = [], []
        for (a, _), (b, _) in zip(snaps, snaps[1:]):
            g = (b - a) / 1000.0
            (reg_gaps if is_regular(a) else off_gaps).append(g)
        counts = [n for _, n in snaps]
        out["types"][key] = {
            "snaps": len(snaps),
            "rows_per_snap_p50": pct(counts, 0.50),
            "rows_per_snap_min": min(counts) if counts else 0,
            "rows_per_snap_max": max(counts) if counts else 0,
            "regular": summarize(reg_gaps),
            "off_hours": summarize(off_gaps),
        }

    # ---- [2] intra-cycle spacing: the 3 types are 3 sequential awaits in ONE
    # body, so the spread between the first and last of a cycle IS the body cost.
    all_snaps = sorted({s for snaps in per_type.values() for s, _ in snaps})
    cycles, cur = [], [all_snaps[0]] if all_snaps else []
    for prev, nxt in zip(all_snaps, all_snaps[1:]):
        if (nxt - prev) / 1000.0 <= 3.0:        # same cycle
            cur.append(nxt)
        else:
            cycles.append(cur)
            cur = [nxt]
    if cur:
        cycles.append(cur)
    full = [c for c in cycles if len(c) == TYPES_PER_CYCLE]
    spans = [(c[-1] - c[0]) / 1000.0 for c in full]
    cycle_starts = [c[0] for c in cycles]
    cycle_gaps_reg, cycle_gaps_off = [], []
    for a, b in zip(cycle_starts, cycle_starts[1:]):
        g = (b - a) / 1000.0
        (cycle_gaps_reg if is_regular(a) else cycle_gaps_off).append(g)

    out["cycle"] = {
        "cycles_seen": len(cycles),
        "cycles_with_all_3_types": len(full),
        "body_span_s_p50": round(pct(spans, 0.50), 3),
        "body_span_s_p90": round(pct(spans, 0.90), 3),
        "body_span_s_max": round(max(spans), 3) if spans else float("nan"),
        "cycle_start_gap_regular": summarize(cycle_gaps_reg),
        "cycle_start_gap_off_hours": summarize(cycle_gaps_off),
    }
    cx.close()
    return out


# --------------------------------------------------------------------------- #
# [3] budget, from the collector log (no API calls)
# --------------------------------------------------------------------------- #
def _kv(line: str, key: str) -> str | None:
    for tok in line.split():
        if tok.startswith(key + "="):
            return tok.split("=", 1)[1]
    return None


def _group_field(line: str, group: str, field: str) -> float | None:
    """Parse `RANKING=peak3/p95:2/avg0.20/tgt4.25/srv10/over0/frn0/frnmax0`."""
    tok = _kv(line, group)
    if tok is None:
        return None
    for part in tok.split("/"):
        if part.startswith(field):
            raw = part[len(field):].lstrip(":")
            try:
                return float(raw)
            except ValueError:
                return None
    return None


def measure_budget(log: str, since_ts: str) -> dict:
    """`since_ts` is a `YYYY-MM-DD HH:MM:SS` prefix cut.

    It exists because this log spans the 2026-08-12 send-time-accounting fix
    (docs/52), and `md_peak_1s` before and after that fix are **different
    quantities**. Mixing them would resurrect the very inflated peak the fix
    removed (73.1% over-limit -> 0.0%).
    """
    with open(log, "r", encoding="utf-8", errors="replace") as fh:
        lines = [ln for ln in fh if "telemetry session=" in ln]
    recent = [ln for ln in lines if ln[:19] >= since_ts]

    out: dict = {"telemetry_lines": len(lines),
                 "first_line_ts": lines[0][:19] if lines else None,
                 "last_line_ts": lines[-1][:19] if lines else None,
                 "recent_cut_ts": since_ts,
                 "recent_lines": len(recent),
                 "by_session": {}, "recent_by_session": {}}

    def collect(src) -> dict:
        buckets: dict[str, dict[str, list[float]]] = {}
        for ln in src:
            b = buckets.setdefault(_kv(ln, "session") or "?", {})
            for name in ("rank_peak_1s", "md_peak_1s", "chart_peak_1s",
                         "over_limit_1s"):
                val = _kv(ln, name)
                if val is None:
                    continue
                try:
                    b.setdefault(name, []).append(float(val))
                except ValueError:
                    pass
            for group in ("RANKING", "MARKET_DATA", "MARKET_DATA_CHART"):
                for field in ("peak", "avg", "frn", "frnmax", "srv"):
                    v = _group_field(ln, group, field)
                    if v is not None:
                        b.setdefault(f"{group}_{field}", []).append(v)
        return {
            sess: {name: {"n": len(vals), "p50": pct(vals, 0.50),
                          "p90": pct(vals, 0.90), "max": max(vals),
                          "mean": round(statistics.fmean(vals), 4)}
                   for name, vals in sorted(b.items())}
            for sess, b in sorted(buckets.items())
        }

    out["by_session"] = collect(lines)
    out["recent_by_session"] = collect(recent)

    # The clamp instrumentation (docs/56) tells us what the SERVER offers per
    # group. RANKING never shows up there, which is itself the answer - see
    # docs/60 for how far that inference goes.
    with open(log, "r", encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()
    groups = [_kv(ln, "limit_header_clamped_groups") for ln in all_lines
              if "limit_header_clamped_groups=" in ln]
    lowered = [_kv(ln, "limit_header_lowered") for ln in all_lines
               if "limit_header_lowered=" in ln]
    # 429 breakdown. `group` is the group that actually took the 429 (from
    # `client.last_429`); `caller` is only the loop that happened to notice it,
    # because `_check_429s` runs in every loop's `finally` (loops.py:804).
    # Reading `caller` as the culprit is a trap, so both are reported.
    det = [ln for ln in all_lines if "HTTP-429-DETAIL" in ln]
    def tally(key: str, src) -> dict:
        counts: dict[str, int] = {}
        for ln in src:
            k = _kv(ln, key) or "?"
            counts[k] = counts.get(k, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    out["http_429_detail"] = {
        "lines": len(det),
        "by_group_that_took_it": tally("group", det),
        "by_caller_that_noticed": tally("caller", det),
        "limit_hdr_values": tally("limit_hdr", det),
        "lines_since_cut": len([ln for ln in det if ln[:19] >= since_ts]),
        "by_group_since_cut": tally("group", [ln for ln in det if ln[:19] >= since_ts]),
    }
    out["server_headers"] = {
        "clamped_groups_seen": sorted({g for g in groups if g and g != "-"}),
        "lines_with_clamp_fields": len(groups),
        "lowered_nonzero_lines": sum(1 for v in lowered if v not in (None, "0")),
        "clamp_log_lines": [ln.split(" - ")[0][:120].strip()
                            for ln in all_lines if "LIMIT-CLAMP" in ln],
    }
    return out


# --------------------------------------------------------------------------- #
# [4] disk - bytes per row, measured not guessed
# --------------------------------------------------------------------------- #
def measure_bytes_per_row(db: str, sample: int) -> dict:
    """Replay real rows into a throwaway replica and weigh it.

    `length()` on the columns would only give the payload; it misses the B-tree
    and both indexes, and `rankings_snap` carries two of them. Rows are copied in
    `id` order so the replica packs the way an append-only table does.
    """
    cx = ro(db)
    c = cx.cursor()
    total_rows = int(c.execute("SELECT count(*) FROM rankings_snap").fetchone()[0])
    max_id = int(c.execute("SELECT max(id) FROM rankings_snap").fetchone()[0])
    rows = c.execute(
        "SELECT id, snap_ms, ranking_type, duration, rank, symbol, last_u, vol_qu,"
        " amount_u FROM rankings_snap WHERE id > ? ORDER BY id",
        (max_id - sample,)).fetchall()
    cx.close()

    def weigh(indexes: tuple[str, ...]) -> tuple[int, int]:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        rx = sqlite3.connect(path)
        rx.execute("PRAGMA page_size=4096")
        rx.execute("PRAGMA journal_mode=OFF")
        rx.execute(RANKINGS_DDL)
        for ddl in indexes:
            rx.execute(ddl)
        rx.executemany("INSERT INTO rankings_snap VALUES (?,?,?,?,?,?,?,?,?)", rows)
        rx.commit()
        pc = int(rx.execute("PRAGMA page_count").fetchone()[0])
        ps = int(rx.execute("PRAGMA page_size").fetchone()[0])
        rx.close()
        os.unlink(path)
        return pc, ps

    pc_idx, ps = weigh(RANKINGS_INDEXES)
    pc_plain, _ = weigh(())
    n = len(rows)
    out = {
        "sample_rows": n,
        "page_size": ps,
        "bytes_per_row_with_indexes": round(pc_idx * ps / n, 2),
        "bytes_per_row_table_only": round(pc_plain * ps / n, 2),
        "bytes_per_row_indexes_only": round((pc_idx - pc_plain) * ps / n, 2),
        "rankings_snap_rows_total": total_rows,
        "rankings_snap_bytes_est": int(total_rows * pc_idx * ps / n),
    }
    # Split the two indexes. The retention proposal in docs/60 rests on this -
    # "drop an index" is only worth proposing if we know which one costs what.
    for ddl in RANKINGS_INDEXES:
        name = ddl.split()[2]
        pc_one, _ = weigh((ddl,))
        out[f"bytes_per_row_{name}"] = round((pc_one - pc_plain) * ps / n, 2)

    # ---- lossless variant: `ranking_type`/`duration` as small integer codes.
    # 'TOSS_SECURITIES_TRADING_VOLUME' is 30 bytes and it is stored in the row,
    # in the UNIQUE autoindex AND in ix_rankings_type_ms - three times per row,
    # for a value drawn from a set of three. Nothing is deleted, so unlike every
    # pruning option this one is reversible and loses no observation.
    codes: dict[tuple[str, str], int] = {}
    coded = []
    for r in rows:
        key = (r[2], r[3])
        codes.setdefault(key, len(codes))
        coded.append((r[0], r[1], codes[key] // 4, codes[key] % 4, *r[4:]))
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    rx = sqlite3.connect(path)
    rx.execute("PRAGMA page_size=4096")
    rx.execute("PRAGMA journal_mode=OFF")
    rx.execute(RANKINGS_DDL.replace("ranking_type TEXT", "ranking_type INTEGER")
               .replace("duration TEXT", "duration INTEGER"))
    for ddl in RANKINGS_INDEXES:
        rx.execute(ddl)
    rx.executemany("INSERT INTO rankings_snap VALUES (?,?,?,?,?,?,?,?,?)", coded)
    rx.commit()
    pc_coded = int(rx.execute("PRAGMA page_count").fetchone()[0])
    rx.close()
    os.unlink(path)
    out["bytes_per_row_int_coded_types"] = round(pc_coded * ps / n, 2)
    out["bytes_per_row_saved_by_int_codes"] = round((pc_idx - pc_coded) * ps / n, 2)
    out["distinct_type_duration_pairs"] = len(codes)
    return out


def measure_promotion_input(db: str, days: float) -> dict:
    """Would 5s feed the promotion path more work? Answered by decimation.

    We cannot sample the future, but we can make the present COARSER. If halving
    the rate (stride 2) barely changes how many distinct symbols we see in the
    top-10, then doubling it cannot add much either - the signal is already
    saturated at this resolution. Same device `docs/35` used to prove the
    "12.5s update period" was our own ruler.

    Only `FEATURE_RANKING_TYPES` matter: `TOP_GAINERS` is stored but deliberately
    kept out of the realtime/promotion path (`loops.py:1685`).
    """
    cx = ro(db)
    c = cx.cursor()
    now_ms = int(c.execute("SELECT max(snap_ms) FROM rankings_snap").fetchone()[0])
    since = now_ms - int(days * 86400_000)
    rows = c.execute(
        "SELECT snap_ms, symbol FROM rankings_snap "
        "WHERE snap_ms >= ? AND rank <= 10 "
        "AND ranking_type = 'TOSS_SECURITIES_TRADING_VOLUME' "
        "ORDER BY snap_ms", (since,)).fetchall()
    cx.close()

    by_snap: dict[int, list[str]] = {}
    for snap_ms, sym in rows:
        by_snap.setdefault(int(snap_ms), []).append(str(sym))
    snaps = sorted(by_snap)

    out = {"window_days": days, "snaps": len(snaps), "strides": {}}
    for stride in (1, 2, 3, 4):
        kept = snaps[::stride]
        syms = {s for m in kept for s in by_snap[m]}
        out["strides"][f"stride_{stride}"] = {
            "effective_period_s": round(stride * 12.6, 1),
            "snaps_used": len(kept),
            "distinct_top10_symbols": len(syms),
        }
    base = out["strides"]["stride_1"]["distinct_top10_symbols"]
    half = out["strides"]["stride_2"]["distinct_top10_symbols"]
    out["halving_the_rate_costs_pct"] = round(100 * (base - half) / base, 2) if base else 0.0
    return out


def measure_tier_caps(log: str, since_ts: str) -> dict:
    """Is the budget governor already squeezing the tiers?

    Matters because ranking entries promote into tier2, and tier2/tier3 are what
    actually spend `MARKET_DATA`. If the caps are already shrunk, extra promotion
    input has nowhere to go; if they are wide open, it does.
    """
    vals: dict[str, list[float]] = {}
    pairs: dict[str, list[tuple[int, int]]] = {}
    with open(log, "r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            if "telemetry session=" not in ln or ln[:19] < since_ts:
                continue
            for name in ("tier2", "tier3", "tier2_cap", "tier3_cap", "watch"):
                v = _kv(ln, name)
                if v is not None:
                    try:
                        vals.setdefault(name, []).append(float(v))
                    except ValueError:
                        pass
            # p50(tier2) == p50(tier2_cap) does NOT mean "at the cap" - the two
            # medians come from different lines. Compare them line by line.
            occ, cap = _kv(ln, "tier2"), _kv(ln, "tier2_cap")
            if occ is not None and cap is not None:
                try:
                    pairs.setdefault(_kv(ln, "session") or "?", []).append(
                        (int(occ), int(cap)))
                except ValueError:
                    pass
    out = {name: {"n": len(v), "p50": pct(v, 0.50), "min": min(v), "max": max(v)}
           for name, v in sorted(vals.items())}
    out["tier2_at_cap_by_session"] = {
        sess: {"n": len(sub),
               "at_cap_pct": round(100 * sum(1 for o, c in sub if o >= c) / len(sub), 1),
               "tier2_p50": pct([o for o, _ in sub], 0.50),
               "cap_p50": pct([c for _, c in sub], 0.50)}
        for sess, sub in sorted(pairs.items()) if sub
    }
    return out


#: Real `rankings_snap` read patterns, quoted from the analysis code. The
#: retention proposal in docs/60 says "look at the indexes first", and that is
#: only worth saying if we know which index each live query actually reaches for.
READ_PATTERNS = (
    ("tick_stages.py:703 (symbol series)",
     "SELECT vol_qu FROM rankings_snap WHERE symbol = ? AND snap_ms >= ?"),
    ("tick_resolution.py:533 (symbol series)",
     "SELECT snap_ms, last_u FROM rankings_snap WHERE symbol = ? AND snap_ms >= ?"),
    ("shot_structure.py:38 (window scan)",
     "SELECT snap_ms, ranking_type, rank, symbol, last_u FROM rankings_snap "
     "WHERE snap_ms >= ? AND snap_ms < ?"),
    ("tick_stages.py:691 (window + duration)",
     "SELECT symbol FROM rankings_snap WHERE snap_ms >= ? AND duration = 'realtime'"),
    ("type + window",
     "SELECT snap_ms FROM rankings_snap WHERE ranking_type = ? AND snap_ms >= ?"),
)


def explain_reads(db: str) -> dict:
    """Which index does each live read pattern actually use? (EXPLAIN, read-only)"""
    cx = ro(db)
    out = {}
    for label, sql in READ_PATTERNS:
        try:
            plan = cx.execute("EXPLAIN QUERY PLAN " + sql,
                              tuple([None] * sql.count("?"))).fetchall()
            out[label] = " | ".join(str(r[-1]) for r in plan)
        except Exception as exc:            # a plan we cannot get is not fatal
            out[label] = f"ERROR {type(exc).__name__}: {exc}"
    cx.close()
    return out


def measure_growth(db: str, days: int) -> dict:
    """Rows/day for `rankings_snap`, most recent `days` full UTC days."""
    cx = ro(db)
    c = cx.cursor()
    now_ms = int(c.execute("SELECT max(snap_ms) FROM rankings_snap").fetchone()[0])
    first_ms = int(c.execute("SELECT min(snap_ms) FROM rankings_snap").fetchone()[0])
    total = int(c.execute("SELECT count(*) FROM rankings_snap").fetchone()[0])
    out = {
        "first_snap_kst": kst(first_ms), "last_snap_kst": kst(now_ms),
        "total_rows": total,
        "span_days": round((now_ms - first_ms) / 86400_000, 3),
        "rows_per_day_lifetime": round(total / ((now_ms - first_ms) / 86400_000), 1),
        "recent": {},
    }
    for d in (1, 3, 7, days):
        since = now_ms - d * 86400_000
        n = int(c.execute("SELECT count(*) FROM rankings_snap WHERE snap_ms >= ?",
                          (since,)).fetchone()[0])
        out["recent"][f"last_{d}d"] = {"rows": n, "rows_per_day": round(n / d, 1)}

    pc = int(c.execute("PRAGMA page_count").fetchone()[0])
    ps = int(c.execute("PRAGMA page_size").fetchone()[0])
    out["db_bytes"] = pc * ps
    out["db_mb"] = round(pc * ps / 1024 / 1024, 1)
    cx.close()

    usage = __import__("shutil").disk_usage(str(Path(db).anchor or "C:/"))
    out["disk_free_bytes"] = usage.free
    out["disk_free_gb"] = round(usage.free / 1024 ** 3, 2)
    out["disk_total_gb"] = round(usage.total / 1024 ** 3, 2)
    return out


# --------------------------------------------------------------------------- #
def render(res: dict) -> None:
    p = print
    p("=" * 78)
    p("[1] RANKING POLL CADENCE (measured, not the config value)")
    p("=" * 78)
    cad = res["cadence"]
    p(f"window: {cad['window_from_kst']} .. {cad['window_to_kst']} KST "
      f"({cad['window_days']} days)")
    p(f"{'type/duration':<40} {'snaps':>7} {'sess':<8} {'p50':>7} {'p90':>7} {'p99':>7}")
    for key, t in cad["types"].items():
        for sess in ("regular", "off_hours"):
            s = t[sess]
            p(f"{key:<40} {t['snaps']:>7} {sess:<8} "
              f"{s['p50']:>7} {s['p90']:>7} {s['p99']:>7}")
    p("")
    cyc = cad["cycle"]
    p(f"cycles seen                 : {cyc['cycles_seen']} "
      f"({cyc['cycles_with_all_3_types']} with all 3 types)")
    p(f"body span (1st..3rd request): p50 {cyc['body_span_s_p50']}s  "
      f"p90 {cyc['body_span_s_p90']}s  max {cyc['body_span_s_max']}s")
    p(f"cycle start gap  regular    : p50 {cyc['cycle_start_gap_regular']['p50']}s  "
      f"p90 {cyc['cycle_start_gap_regular']['p90']}s  "
      f"n={cyc['cycle_start_gap_regular']['n_within_cap']}")
    p(f"cycle start gap  off-hours  : p50 {cyc['cycle_start_gap_off_hours']['p50']}s  "
      f"p90 {cyc['cycle_start_gap_off_hours']['p90']}s  "
      f"n={cyc['cycle_start_gap_off_hours']['n_within_cap']}")

    p("")
    p("=" * 78)
    p("[2] PROJECTION to ranking_snap_s = 5")
    p("=" * 78)
    proj = res["projection"]
    for k, v in proj.items():
        p(f"  {k:<38}: {v}")

    p("")
    p("=" * 78)
    p("[3] API BUDGET (from collector.log telemetry, zero API calls)")
    p("=" * 78)
    bud = res["budget"]
    p(f"telemetry lines: {bud['telemetry_lines']}  "
      f"({bud['first_line_ts']} .. {bud['last_line_ts']})")

    def show(title: str, table: dict) -> None:
        p(f"  --- {title} ---")
        p(f"  {'session':<9} {'n':>5} {'RANK avg':>9} {'RANK pk':>8} "
          f"{'RANK frnmax':>12} {'md_peak_1s':>11} {'over_limit':>11}")
        for sess, f in table.items():
            r = f.get("RANKING_avg")
            if not r:
                continue
            pk, frn = f.get("RANKING_peak", {}), f.get("RANKING_frnmax", {})
            md, ov = f.get("md_peak_1s", {}), f.get("over_limit_1s", {})
            p(f"  {sess:<9} {r['n']:>5} "
              f"{str(r['p50']) + '/' + str(r['max']):>9} "
              f"{str(pk.get('p50')) + '/' + str(pk.get('max')):>8} "
              f"{str(frn.get('n', 0)) + 'ln max' + str(frn.get('max')):>12} "
              f"{str(md.get('p50')) + '/' + str(md.get('max')):>11} "
              f"{str(ov.get('p50')) + '/' + str(ov.get('max')):>11}")

    p("  (cells are p50/max unless noted)")
    show(f"whole log  {bud['first_line_ts']} ..", bud["by_session"])
    show(f"post send-time fix, since {bud['recent_cut_ts']} "
         f"({bud['recent_lines']} lines)", bud["recent_by_session"])
    sh = bud["server_headers"]
    p(f"  groups the server offers ABOVE our ceiling : "
      f"{sorted({g.split(':')[0] for s in sh['clamped_groups_seen'] for g in s.split(',')})}")
    p(f"  telemetry lines carrying clamp fields      : {sh['lines_with_clamp_fields']}")
    p(f"  lines with limit_header_lowered != 0       : {sh['lowered_nonzero_lines']}")
    p(f"  LIMIT-CLAMP log lines                      : {len(sh['clamp_log_lines'])}")
    d4 = bud["http_429_detail"]
    p(f"  HTTP-429-DETAIL lines                      : {d4['lines']} "
      f"({d4['lines_since_cut']} since cut)")
    p(f"    group that TOOK the 429                  : {d4['by_group_that_took_it']}")
    p(f"    caller that merely NOTICED it            : {d4['by_caller_that_noticed']}")
    p(f"    limit_hdr on the 429 response            : {d4['limit_hdr_values']}")
    p(f"    since cut, by group                      : {d4['by_group_since_cut']}")

    p("")
    p("=" * 78)
    p("[3b] PROMOTION INPUT - does a finer ruler feed the tier path more?")
    p("=" * 78)
    pi = res["promotion_input"]
    p(f"  TOSS_SECURITIES_TRADING_VOLUME top-10, {pi['window_days']}d, "
      f"{pi['snaps']} snaps")
    for k, v in pi["strides"].items():
        p(f"  {k:<10} eff {v['effective_period_s']:>6}s  "
          f"snaps {v['snaps_used']:>7}  distinct top-10 symbols "
          f"{v['distinct_top10_symbols']:>6}")
    p(f"  halving the poll rate loses only {pi['halving_the_rate_costs_pct']}% "
      f"of the distinct top-10 set")
    p("  tier occupancy / governor caps (post-fix window):")
    for name, v in res["tier_caps"].items():
        if name == "tier2_at_cap_by_session":
            continue
        p(f"    {name:<12} p50 {v['p50']:>7}  min {v['min']:>7}  max {v['max']:>7}")
    p("  is tier2 actually AT its cap? (line-by-line, not median-vs-median)")
    for sess, v in res["tier_caps"]["tier2_at_cap_by_session"].items():
        p(f"    session={sess:<8} n={v['n']:<4} at-cap {v['at_cap_pct']:>5}%  "
          f"tier2 p50 {v['tier2_p50']:>6}  cap p50 {v['cap_p50']:>6}")

    p("")
    p("=" * 78)
    p("[4] DISK")
    p("=" * 78)
    for k, v in res["disk"]["bytes"].items():
        p(f"  {k:<38}: {v}")
    g = res["disk"]["growth"]
    p(f"  {'db_mb':<38}: {g['db_mb']}")
    p(f"  {'rankings rows total':<38}: {g['total_rows']}")
    p(f"  {'rankings span days':<38}: {g['span_days']}")
    p(f"  {'rows/day lifetime':<38}: {g['rows_per_day_lifetime']}")
    for k, v in g["recent"].items():
        p(f"  {'rows/day ' + k:<38}: {v['rows_per_day']}")
    for k, v in res["disk"]["projection"].items():
        p(f"  {k:<38}: {v}")
    p("  which index each live read actually uses:")
    for label, plan in res["read_plans"].items():
        p(f"    {label:<42} {plan}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--days", type=float, default=3.0,
                    help="cadence window (days back from the newest snap)")
    ap.add_argument("--growth-days", type=int, default=14)
    ap.add_argument("--sample-rows", type=int, default=300_000)
    ap.add_argument("--target-s", type=float, default=5.0)
    ap.add_argument("--configured-s", type=float, default=12.0,
                    help="polling.ranking_snap_s in the LIVE config right now")
    ap.add_argument("--since", default="2026-08-12 22:30:00",
                    help="cut for the post-send-time-fix telemetry window (docs/52)")
    ap.add_argument("--db-growth-gb-day", type=float, default=0.398,
                    help="whole-DB growth measured by the coordinator 2026-08-13")
    args = ap.parse_args()

    cadence = measure_cadence(args.db, args.days)
    budget = measure_budget(args.log, args.since)
    bytes_row = measure_bytes_per_row(args.db, args.sample_rows)
    growth = measure_growth(args.db, args.growth_days)
    promo = measure_promotion_input(args.db, args.days)
    caps = measure_tier_caps(args.log, args.since)
    plans = explain_reads(args.db)

    # ---- projection. `_loop` sleeps AFTER the body (loops.py:1591), so the real
    # period is `configured + overhead`, which is why config 12 measures ~12.6.
    # The overhead is body work (3 requests + 3 writes + universe resolve + budget
    # + state save); it does not scale with the sleep, so it carries over intact.
    now_cycle = cadence["cycle"]["cycle_start_gap_regular"]["p50"]
    overhead = now_cycle - args.configured_s
    span = cadence["cycle"]["body_span_s_p50"]
    new_cycle = args.target_s + overhead
    mult = now_cycle / new_cycle
    rows_day_recent = growth["recent"]["last_3d"]["rows_per_day"]
    bpr = bytes_row["bytes_per_row_with_indexes"]
    projection = {
        "configured_period_s": args.configured_s,
        "measured_cycle_s_now": round(now_cycle, 3),
        "loop_overhead_s": round(overhead, 3),
        "first_to_third_request_span_s": round(span, 3),
        "projected_cycle_s_at_target": round(new_cycle, 3),
        "naive_multiplier_config_only": round(args.configured_s / args.target_s, 3),
        "real_multiplier": round(mult, 3),
        "ranking_req_per_s_now": round(TYPES_PER_CYCLE / now_cycle, 4),
        "ranking_req_per_s_at_target": round(TYPES_PER_CYCLE / new_cycle, 4),
        "ranking_ceiling_req_per_s": RANKING_CEILING,
        "ranking_target_req_per_s": RANKING_CEILING * USAGE_RATIO,
        "pct_of_ceiling_now": round(100 * TYPES_PER_CYCLE / now_cycle / RANKING_CEILING, 2),
        "pct_of_ceiling_at_target": round(100 * TYPES_PER_CYCLE / new_cycle / RANKING_CEILING, 2),
        "rows_per_s_now": round(TYPES_PER_CYCLE * ROWS_PER_REQUEST / now_cycle, 2),
        "rows_per_s_at_target": round(TYPES_PER_CYCLE * ROWS_PER_REQUEST / new_cycle, 2),
    }
    gb_rank_now = rows_day_recent * bpr / 1024 ** 3
    gb_rank_new = rows_day_recent * mult * bpr / 1024 ** 3
    # `--db-growth-gb-day` is the whole-DB growth the coordinator measured on
    # 2026-08-13 (COORDINATOR-STATE 2-1). Everything that is not rankings keeps
    # growing at the same rate, so the non-ranking remainder is carried over.
    other = max(args.db_growth_gb_day - gb_rank_now, 0.0)
    free_gb = growth["disk_free_gb"]
    disk_projection = {
        "rows_per_day_now_measured": rows_day_recent,
        "rows_per_day_at_target": round(rows_day_recent * mult, 1),
        "bytes_per_row_measured": bpr,
        "gb_per_day_rankings_now": round(gb_rank_now, 4),
        "gb_per_day_rankings_at_target": round(gb_rank_new, 4),
        "gb_per_day_delta": round(gb_rank_new - gb_rank_now, 4),
        "gb_per_day_db_total_reference": args.db_growth_gb_day,
        "gb_per_day_non_ranking_remainder": round(other, 4),
        "gb_per_day_db_total_at_target": round(other + gb_rank_new, 4),
        "rankings_share_of_db_pct": round(
            100 * bytes_row["rankings_snap_bytes_est"] / growth["db_bytes"], 1),
        "free_gb": free_gb,
        "days_to_full_now": round(free_gb / args.db_growth_gb_day, 1),
        "days_to_full_at_target": round(free_gb / (other + gb_rank_new), 1),
    }
    # What each index costs, and what dropping it would buy at the target rate.
    # Reclaim is one-off (existing rows) + ongoing (new rows).
    rows_target = rows_day_recent * mult
    total_rows = bytes_row["rankings_snap_rows_total"]
    for key, val in list(bytes_row.items()):
        if not key.startswith("bytes_per_row_ix_"):
            continue
        name = key[len("bytes_per_row_"):]
        one_off = total_rows * val / 1024 ** 3
        ongoing = rows_target * val / 1024 ** 3
        disk_projection[f"drop_{name}_reclaim_gb_now"] = round(one_off, 3)
        disk_projection[f"drop_{name}_saves_gb_per_day_at_target"] = round(ongoing, 4)
        disk_projection[f"drop_{name}_days_to_full_at_target"] = round(
            (free_gb + one_off) / (other + gb_rank_new - ongoing), 1)
    saved = bytes_row["bytes_per_row_saved_by_int_codes"]
    one_off = total_rows * saved / 1024 ** 3
    ongoing = rows_target * saved / 1024 ** 3
    disk_projection["int_codes_reclaim_gb_now"] = round(one_off, 3)
    disk_projection["int_codes_saves_gb_per_day_at_target"] = round(ongoing, 4)
    disk_projection["int_codes_days_to_full_at_target"] = round(
        (free_gb + one_off) / (other + gb_rank_new - ongoing), 1)

    res = {"cadence": cadence, "budget": budget,
           "disk": {"bytes": bytes_row, "growth": growth,
                    "projection": disk_projection},
           "promotion_input": promo, "tier_caps": caps, "read_plans": plans,
           "projection": projection}
    render(res)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

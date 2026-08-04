# W4 — symbol_not_found split (task_8280709b754a)

Branch `w4-collector`, commit `4397130`. Tests: **1382 passed / 1 skipped**.

Scope was cut mid-task by the coordinator: part (2) (429 / tier-cap shrink) was dropped
because a second W4 terminal was already executing a more detailed 429 spec in this same
worktree (their commit `dd51602`). This report covers part (1) plus findings that fall
outside my ownership.

---

## (a) What I did

**The defect.** `client._classify` raises `SchemaMismatch` for every non-retryable 4xx —
contract C-5 has no row for 4xx, so "no retry + caller logs and skips" got mapped onto the
same exception as "the response shape changed". `_guarded` counted the whole exception class
as `schema_mismatch`, and that counter is what raises

> the API response shape changed. A restart will NOT fix this. Inspect the endpoint
> contract before trusting today's data

**Measured**: across the whole of `collector.log` (2026-07-31 -> 08-04), `schema_mismatch`
fired exactly **5 times, and all 5 were `http-404 code=stock-not-found`**:

```
07-31 20:40:55 tier2:AVAT    08-03 23:16:12 tier2:BLRK    08-03 23:16:26 tier2:CABR
08-03 23:18:13 tier2:MACI    08-03 23:21:31 tier2:JSM
```

5 distinct symbols, once each, all on the tier2 candle path. The counter has **never** been
raised by an actual contract change. Every alert it produced was false, and the message told
a human to distrust a good day of data.

**The change** (`tossmon/collector/mismatch.py`, new + 42 lines in `loops.py`):

| Piece | What it does |
|---|---|
| `parse_http_detail` / `is_symbol_not_found` | Splits the class. Anchored at `^http-<status>` — see below. |
| `_guarded` | 404 + `stock-not-found` -> `symbol_not_found`, skip that symbol only, log that it is *not* a contract change (ASCII). Everything else still -> `schema_mismatch`. |
| `telemetry()` | `symbol_not_found` and `symbol_not_found_symbols` exposed separately from `schema_mismatch`. |
| `NotFoundTally` | Per-symbol 404 counts, bounded at 512 symbols with a visible `untracked=` overflow. |
| `_report_repeat_not_found` | Names symbols seen >=2 times in the periodic report. Silent when there is no repeat. |

**Why the split condition is anchored, not a substring match.** Nested field errors are
wrapped as `f"{where}.{key}: {exc.detail}"`, and response body values land inside `detail`.
A `"stock-not-found" in detail` test would therefore misclassify a *real* shape change as a
missing symbol — the exact failure direction we are trying to prevent, but silent. So the
parser only reads a status/code when `detail` **starts with** `http-<status>`. Both the
status **and** the code must match.

A 404 whose body we cannot read (`<unparseable>`, `<no error field>`, `error=<str>`) stays
`schema_mismatch`: there is no evidence it was a missing symbol, and "cannot judge" belongs
on the suspicious side.

`symbol_not_found` and `symbol_not_found_symbols` are reported separately on purpose — one
symbol 404ing 40 times means "clean up the watchlist", 40 symbols 404ing once each means
"suspect the contract after all". A single count cannot tell those apart.

**Not done, deliberately** (coordinator's instruction): repeat-404 symbols are *reported*,
never auto-dropped from the watchlist. Alert wording and severity are W5's — `ops/**`
untouched.

## (b) How I verified it

- Wrote the regression tests first and ran them against unmodified `loops.py`:
  **43 passed / 4 failed**, the 4 failures being exactly the counter split, the per-symbol
  tally, and the telemetry keys. Then applied the fix: **48 passed**. (No stash needed —
  the tests are a new file, so the pre-fix run *is* the failure demonstration.)
- Both directions pinned, per spec: 404 with a different code, `stock-not-found` with a
  different status (400/409/422/200), and 7 real shape-change details all stay
  `schema_mismatch`; 5 substring lookalikes do not leak into `symbol_not_found`.
- Full suite: **1382 passed / 1 skipped** (green; baseline on main was 1328/1, the delta is
  my 48 plus the other W4 terminal's tests).
- The live detail string used in tests is copied verbatim from `collector.log`, trailing
  period included.
- No contact with the running collector, no live API calls. I read
  `w5-ops/data/collector.log` read-only.

## (c) What I could not do

- **Part (2) was dropped on the coordinator's instruction**, not because it was blocked. A
  second W4 terminal was mid-flight in the same worktree on a more detailed 429 spec; I
  stopped before editing and escalated rather than clobbering their work. Their `dd51602`
  landed the recovery ratchet, `RATE_LIMITED_SHRINK_FRAC` 0.2 -> 0.10, 429 header capture,
  and the session-transition baseline burst fix.
- I never determined *why* the server returns 429 while we sit at 15-20% of the limit. It
  needs the raw 429 headers, which nothing logged until `dd51602` added
  `HTTP-429-DETAIL`. The next open session's log will answer it.

## (d) Findings outside my ownership

**1. The spec's premise about session hours is wrong, and it matters.** The spec says
"there is not a single 429 between 22:30 and 08:50 KST" and warns that the tier3 die-off
"would recur if the same thing happened right after the 22:30 open". The 429 claim is
correct. But the *outcome* it warns about is **already happening at every open**, through a
different trigger:

```
07-31 22:52:55  budget shrink {'MARKET_DATA': 2} -> tier3:18  demoted=1
07-31 22:53:31  budget shrink {'MARKET_DATA': 3} -> tier3:15  demoted=3
08-03 22:34:36  budget shrink {'MARKET_DATA': 2} -> tier3:18  demoted=4
08-03 22:35:08  budget shrink {'MARKET_DATA': 4} -> tier3:14  demoted=8
08-04 02:48:01 / 02:49:06                        -> tier3:16
```

No `budget: 429 on ...` line precedes any of these (nearest prior 429 is ~7 hours earlier),
so `_forced` was empty — these came from the `measured > target * HEADROOM` branch. tier3
went 20 -> 14 within 32 seconds of the 08-03 open, evicting members scoring 0.612, 0.633,
0.645, 0.653. Filtering the log for `429` hides this entirely; filter on `budget shrink`.

**2. Why that branch trips at the open.** The config plan sits 3.3% below the shrink
trigger:

```
planned MARKET_DATA @ tier3_max=20 = 1500/200/45 + 20/4 + 20/16 = 6.4167 req/s
target            = 10 * 0.7          = 7.00
shrink trigger    = target * HEADROOM = 6.65      <- measured is compared against this
margin                                = 0.233 req/s
```

`validate_plan()` checks the plan against `target` (7.00) and passes; `should_shrink()`
compares measured against `target * 0.95` (6.65). A plan can therefore pass startup
validation while already sitting inside the auto-shrink zone. Any retry, any
`_resolve_watchlist_unknowns` batch, any promotion backfill crosses 6.65. The
`config.example.yaml` comment ("6.42 <= 7.0, OK") is validating against the wrong number.

**3. One 429 can be charged twice, to the wrong groups.** `sync_rate_limits(group)` reads
the **global** `client.counters["http_429"]` and attributes the increment to *the calling
loop's* group. And `_request` bumps that counter on both the initial 429 and its one retry.
So a single rate-limited request can produce two forced shrinks in two unrelated tiers.
Evidence: `08-03 11:00:07 429 on MARKET_DATA_CHART (count=1)` then `11:00:08 429 on
MARKET_DATA (count=1)` — one second apart, which is exactly the observed `Retry-After: 1`.
`429 on RANKING` also appears twice, and RANKING is documented as never shrinkable.
Both `budget.py` and `loops.py` are W4-owned, so this is for whoever holds the 429 task.

**4. `RateLimited` discards the server's error code.** `_classify` raises
`RateLimited(_retry_after(headers), "rate limited")` — it drops `_err_code(resp)`. Toss
documents two distinct codes, `rate-limit-exceeded` (our per-key quota) and
`edge-rate-limit-exceeded` (shared edge, `docs/06_live_facts.md` §9-2, unobserved so far).
Those mean opposite things for "is our limit model wrong", and we cannot currently tell
them apart. `tossmon/api/**` is W1's.

## (e) What the next person should know

- **Read the two counters separately.** `schema_mismatch > 0` still means "distrust today's
  data". `symbol_not_found > 0` is normal — delisted, halted, or renamed tickers appearing
  in a ranking. If `symbol_not_found_symbols` suddenly spreads across many symbols instead
  of repeating on a few, go back to suspecting the contract.
- **W5 hand-off**: the counter is `symbol_not_found` in the telemetry line, alongside
  `symbol_not_found_symbols`. `schema_mismatch` keeps its meaning and should stay CRIT;
  `symbol_not_found` should drop to NOTE. I did not touch `ops/**`.
- Repeat offenders are printed as `symbol-not-found repeats: SYM=n ...` in the 5-minute
  report, only when some symbol has been seen twice or more. Nothing is auto-removed from
  the watchlist — that decision is still open.
- **Two agents shared this worktree.** If `git status` shows uncommitted work in
  `tossmon/collector/**`, check `ls -la` mtimes before editing. Mine is committed at
  `4397130`; theirs at `dd51602`.
- The live collector runs from `w5-ops/`, not from this worktree — its log is
  `orca/workspaces/toss_trade/w5-ops/data/collector.log` (~20 MB). That is the only place
  the real telemetry history lives.

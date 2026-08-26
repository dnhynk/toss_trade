# 73. Budget ceiling - the visible 20 is per-second, but useful placebo seats do not fit

Date: 2026-08-27

## 0. Verdict

**Verdict: (b) it does not fit.**

The stored evidence settles the narrow header question:

- `MARKET_DATA_CHART=20` is the limit of a visible **one-second** server bucket. It is not
  a per-minute reinterpretation.
- `MARKET_DATA=15` has the same one-second meaning. The collector correctly clamps it to
  the unchanged local ceiling of 10.
- Neither number is permission to raise a ceiling. Stored 429 responses prove that a
  second, unadvertised limiter rejects requests while the visible bucket still has ample
  remaining quota. Its dimension and usable envelope remain unresolved.

The seat question is smaller than the old 20-seat upper bound, but not small enough:

- The current plan can buy **at most two** additional tier3 seats:
  `2 * 0.5833 = 1.1667 req/s`, leaving only `0.0472 req/s` below the plan ceiling.
- With hindsight, the frozen 108 placebo draws need at most two simultaneous seats.
  That is not a causal policy: 58 draws are before the first entry, and a future candle
  match is not known before its `tau` has arrived.
- The smallest causal fixed hold tested, 15 minutes beginning at `t0 + 600s`, already
  needs **three** simultaneous seats (`1.7500 req/s`) and covers only 6 of the 79 events.
  It exceeds the plan headroom by `0.5361 req/s`.
- A 60-minute hold reaches 18 events, the size of the old first-entry bias sample, but
  needs six simultaneous seats (`3.5000 req/s`). A hold until three matches or session
  close needs 16 (`9.3333 req/s`).

Therefore the 20-seat continuous-rank design was a large and misdirected upper bound, but
replacing it with a causal short-lived design does **not** rescue the seat axis under the
current budget. No ceiling, alert threshold, cadence, collector, config, or watchdog was
changed.

## 1. Scope and data boundary

This work is calculation and judgement only.

| Item | Boundary |
|---|---|
| Live API calls | **0** |
| Collector/config/watchdog/deployment | **untouched** |
| DB | `file:.../tossmon.db?mode=ro`, via `hires_events.open_ro` |
| Outcome data | exploration B only: 2026-08-18, 19, 20, 21, 24, 25 |
| Holdout | 2026-05-01 through 2026-07-29 **not opened** |
| Confirmation outcomes | 2026-08-26 onward **not opened** |
| Current operations evidence | stored log telemetry only; used for quota, never returns |

The event and placebo definitions are unchanged from `docs/69` through `docs/71`:

- cell: `TOSS_SECURITIES_TRADING_VOLUME / realtime / E1_new_entry / N10`
- stratum: `anchored AND first_in_regular`
- events: 79 in six exploration-B regular sessions
- placebo: same symbol and regular session, `abs(tau - t0) > 600s`, candle key defined,
  `crv5 +/-20%`, `cvol5 x1.2`
- tape window priced here: `[tau, tau + 300s]`
- per-seat traffic: trades every 3 seconds plus orderbook every 4 seconds

## 2. What the advertised 20 means

### 2.1 Stored header evidence

The old one-second contract was measured before the raise (`docs/06` sec. 9): in a fixed
server second, the k-th request returned `remaining = limit - k`, and the counter reset at
the next wall-clock second.

The stored post-raise evidence preserves that shape:

1. `docs/55` sec. 2 contains 332 true `/api/v1/candles` 429 records:

   | visible limit | span | records |
   |---:|---|---:|
   | 5 | 2026-08-10 09:00:22 to 2026-08-11 12:31:11 | 203 |
   | 15 | 2026-08-11 12:43:37 to 12:44:01 | 2 |
   | 20 | 2026-08-11 12:51:08 to 2026-08-13 11:01:19 | 127 |

   All 332 records have `x-ratelimit-reset=1`. The values form three time-contiguous
   steps; they are not three simultaneous buckets or a path-mixing artifact.

2. The current stored `collector.log` has 53 true 429 records from 2026-08-18 through
   2026-08-26. Reading only the response headers on those stored lines gives:

   | group | limit | reset | records | visible consumption at rejection | paths |
   |---|---:|---:|---:|---:|---|
   | `MARKET_DATA` | **15** | **1** | 36 | 1 through 5 of 15 | prices, trades, orderbook |
   | `MARKET_DATA_CHART` | **20** | **1** | 17 | 1 through 2 of 20 | candles |

3. Stored `LIMIT-CLAMP start` lines repeat the same pair in 12 first-response/process
   episodes from 2026-08-13 through 2026-08-24:

   ```text
   MARKET_DATA       server=15.0 ceiling=10.0
   MARKET_DATA_CHART server=20.0 ceiling=5.0
   ```

   There is no stored stop transition or alternate maximum in those episodes.

### 2.2 Why `15 > 10` is not a per-minute quota

The per-minute hypothesis fails four independent checks.

1. **Magnitude.** Converting the old published limits to a minute denominator would give
   `MARKET_DATA=600/min` and `CHART=300/min`, not 15 and 20.
2. **Group ratios.** The observed changes are different by group: `10 -> 15` is 1.5x,
   while `5 -> 20` is 4x. A denominator change would multiply both by 60.
3. **Reset.** Both new values still travel with `x-ratelimit-reset=1`, including all 53
   recent true 429 responses. A minute bucket did not appear in the reset field.
4. **Time structure.** The CHART value stepped 5 -> 15 -> 20 over about 20 minutes and
   then stayed at 20. A semantic switch would not normally take the form of two numeric
   raises while retaining the old reset and remaining fields.

**Finding:** the visible server buckets were raised to 15/s and 20/s. The local clamp is
cutting a per-second advertised value, not a minute quota expressed with a new denominator.

### 2.3 What the 20 does not mean

The same stored records show why `20/s advertised` is not `20/s usable`.

- All 332 historical CHART 429s were rejected after only 1 through 4 visible-bucket
  requests.
- The recent records are rejected at only 1 through 5 of 15 for MARKET_DATA and 1 through
  2 of 20 for CHART.
- The body is still `rate-limit-exceeded`; the rejecting limiter does not identify itself
  in another header.

Therefore at least two server limiters exist: the visible one-second bucket described by
`X-RateLimit-*`, and an unadvertised rejector. Stored data settles the **unit and meaning of
the visible 15/20**, but cannot settle the dimension or usable ceiling of the second
limiter. This is why no local ceiling can be raised from these findings.

## 3. Re-deriving the seat need

### 3.1 Counting method

The existing `candle_ladder` runner was replayed against the read-only DB. For each of the
79 events, the unchanged `placebo_candidate_mask` produced every qualifying `tau`.

For seat pricing:

1. each selected `tau` creates `[tau, tau + 300s]`;
2. duplicate draws are deduplicated;
3. overlapping intervals for the same symbol and session are merged;
4. a sweep of interval starts and ends gives maximum simultaneous symbols;
5. seat-seconds are the sum of the merged intervals.

The calculation does not assume that every minute can be known in advance. Hindsight and
causal schedules are reported separately.

### 3.2 Hindsight lower bounds

| Hindsight target | Raw windows | Unique/merged | Events | Seat-seconds | Max seats | Peak incremental req/s |
|---|---:|---:|---:|---:|---:|---:|
| Frozen 108 draws | 108 | 77 / 56 | 36 | **18,780** | **2** | **1.1667** |
| Frozen draws after `t0` only | 50 | 36 / 26 | 21 | 8,580 | 2 | 1.1667 |
| First 3 future candidates/event | 59 | 59 / 37 | 26 | 12,960 | 2 | 1.1667 |
| Every qualifying future candidate | 93 | 93 / 46 | 26 | 17,580 | 2 | 1.1667 |
| Every qualifying candidate, past and future | **173** | 173 / 92 | **36** | **34,380** | **3** | **1.7500** |

This replaces the old `ranks 11-30 continuously = 20 seats` assumption with the actual
geometry. Even every observed qualifying window needs only three simultaneous symbols in
hindsight, not 20.

It also reveals a different limit: only **36/79** events have any candidate under the
frozen bands, and only **26/79** have a future candidate. Seats cannot create a matching
candle state where the `crv5/cvol5` bands produce none. The seat axis can repair missing
tape, not the empty-band half of the funnel.

### 3.3 Why the two-seat result is not deployable as stated

The two-seat rows above assume the exact candidate `tau` is known at `tau`. It is not.

- A past candidate can be labeled as belonging to a first-entry symbol only after that
  first entry occurs. In the frozen 108 draws, 58 are before `t0`.
- A future candidate is defined by the candle state at `tau`. That state is not complete
  before `tau`, and the existing tier2 candle loop polls every 110 seconds. Starting a seat
  after observing the match misses the beginning of `[tau, tau + 300s]` and changes the
  tape ruler.
- An oracle hold immediately before first entry is not a policy. With the 600-second self
  gap, even a 15-minute oracle lookback has only a five-minute eligible slice and finds
  candidates for just 4/79 events. Lookbacks of 30, 60, and 120 minutes reach only 8, 12,
  and 16 events.

Thus the hindsight concurrency is a lower bound, not an implementation plan.

### 3.4 Causal holds

A causal design must pre-seat an already-known first-entry symbol before a future match can
be recognized. The replay starts a hold at `t0 + 600s`; this is the first eligible placebo
time. It either holds for a fixed duration or until matches have been captured.

Only the 71 events with a defined event key are eligible; one occurs too near session close
to start the hold, leaving 70 intervals.

| Causal policy | Events with captured candidate | Candidate windows captured | Seat-seconds | Max seats | Peak incremental req/s |
|---|---:|---:|---:|---:|---:|
| Hold 15 min | 6/79 | 10/93 | 62,297 | **3** | **1.7500** |
| Hold 30 min | 11/79 | 25/93 | 120,695 | 4 | 2.3333 |
| Hold 60 min | **18/79** | 56/93 | 232,143 | **6** | **3.5000** |
| Hold 120 min | 20/79 | 75/93 | 434,797 | 9 | 5.2500 |
| Hold to first match or close | 26/79 | 26/93 | 609,741 | 11 | 6.4167 |
| Hold to 3 matches or close | 26/79 | 59/93 | 726,861 | **16** | **9.3333** |

The 15-minute policy is the cheapest causal policy in the table and already needs three
seats while capturing almost nothing. A 60-minute hold is the first row to reach 18 events,
the size of the old first-entry bias sample, and costs six simultaneous seats. Waiting for
three matches is dominated by events that never match and therefore remain seated to the
close.

## 4. Price and current headroom

### 4.1 Per-seat price and planning ceiling

From `docs/61` sec. 4-1 and `TierPlan.rates()`:

```text
one tier3 seat = 1/3 trades + 1/4 orderbook = 7/12 = 0.583333 req/s
base plan       = 8/45 + 10/3 + 10/4       = 6.011111 req/s
plan ceiling    = 8.5 * (0.95 - 0.10)      = 7.225000 req/s
plan headroom   = 7.225000 - 6.011111      = 1.213889 req/s
```

| Extra seats | Increment | Planned total | Headroom after | Fits plan ceiling? |
|---:|---:|---:|---:|---|
| 1 | 0.5833 | 6.5944 | 0.6306 | yes |
| **2** | **1.1667** | **7.1778** | **0.0472** | **yes, barely** |
| **3** | **1.7500** | **7.7611** | **-0.5361** | **no** |
| 6 | 3.5000 | 9.5111 | -2.2861 | no |
| 16 | 9.3333 | 15.3444 | -8.1194 | no; also 5.3444 above local 10/s |

The planning result is unchanged from `docs/61`: two is the maximum. Conditional duty
reduces average traffic, but the budget must still survive the maximum simultaneous seat
count if tape cadence is to be guaranteed.

### 4.2 Real current telemetry

Stored current-process regular-session telemetry was read from 2026-08-24 22:30:04 through
2026-08-27 00:15:35 (178 five-minute lines). This is operational quota evidence only;
no confirmation outcome row was read.

| Metric | min | p50 | p95 | max |
|---|---:|---:|---:|---:|
| `md_peak_1s` | 9 | **10** | **10** | **10** |
| `md_p95_1s` | 6 | 8 | 9 | 10 |
| 60-second measured `avg` | 3.32 | 5.03 | **5.78** | **6.13** |
| actual tier3 members | 5 | 8 | 10 | 10 |
| `tier3_cap` | 10 | 10 | 10 | 10 |

Additional facts:

- `md_peak_1s == 10` in **97.8%** of the 178 telemetry windows.
- Measured-rate headroom to the 7.225 plan ceiling is `1.445 req/s` at p95 and
  `1.095 req/s` at the observed maximum.
- Instantaneous hard-cap headroom is **0** at p50, p95, and max of `md_peak_1s`.
- Tier3 has two empty slots at its median, but zero at p95. Vacant nominal seats are
  intermittent, not a guarantee at a target placebo minute.

There is no single honest number called current headroom. Sustained telemetry has some
room, the planning reserve buys two seats by 0.0472 req/s, and the one-second peak has no
room. The useful causal policies need at least three seats, so all three readings point to
the same decision: do not add them.

The 53 recent stored 429s all occurred in the `day` session, not regular. That is favorable
for the target window but not proof of regular-session capacity: the hidden rejector is
still unidentified, and absence of a rejection is not a measured ceiling.

## 5. Decision and what remains open

### 5.1 Closed in this task

- The header denominator question is closed for the visible bucket: **15/s and 20/s**.
- The per-minute interpretation is rejected by stored reset, remaining, magnitude, and
  transition evidence.
- The continuous 20-seat assumption is rejected as the wrong cost model.
- Under the current plan, **two** seats are the numerical maximum.
- No causal policy that materially covers the 79-event placebo problem fits those two
  seats. The seat expansion axis is therefore closed for this measurement.

### 5.2 Still open

- The second server limiter's dimension and effective usable envelope.
- Whether a quota-aware two-seat **sampling** policy over more sessions is worth a new
  preregistration. It would measure a selected subset, not cover the six-session 79-event
  population, and it cannot reuse the current frozen ruler without addressing detection
  lag.
- Whether to change the matching bands or the ruler. Seats do not solve the fact that
  43/79 events have no qualifying candidate.

## 6. Probe design - do not fire without a separate user decision

No probe is needed to determine the visible header unit. A probe is needed only if the
user wants to discover the unadvertised rejector or authorize capacity above the current
envelope.

### 6.1 Stage A: exact two-seat shape under the unchanged local cap

Purpose: learn whether the only numerically affordable design shape can keep its deadlines
without increasing any ceiling.

1. The collector remains the sole API owner; do not issue or refresh a token elsewhere.
2. Mark the probe window in data and logs so it is excluded from strategy and `foreign`
   inference.
3. For a pre-approved regular-session interval, add the exact traffic shape of two seats:
   per seat, one trades request every 3 seconds and one orderbook request every 4 seconds.
4. Keep the existing 10/s limiter and every ceiling/threshold unchanged.
5. Record every send and response, not five-minute aggregates: monotonic send time, server
   `Date`, path, group, status, `limit`, `remaining`, `reset`, error code, request id,
   retry, queue delay, and missed poll deadline.
6. Abort on the first 429, auth anomaly, collector stall, tape gap, or unapproved call
   count. A delayed request counts as failure if it misses the frozen tape cadence even
   when the limiter prevents a 429.

Success is not merely `429 == 0`; both added seats must retain their 3s/4s deadlines for
the pre-registered duration.

### 6.2 Stage B: identify the hidden limiter

This stage deliberately risks 429 and must be a separate decision with an exclusive
collector-owned lease and a fixed call budget.

Use short, randomized, repeated blocks that vary one axis at a time:

| Axis | Paired blocks | What a shift would mean |
|---|---|---|
| MARKET_DATA rate | serial 6, 8, 10, then user-approved 11 through 15/s | hidden group rate threshold |
| Path mix | same total on trades only vs trades/orderbook/prices split | path-specific limiter |
| Cross-group load | same MARKET_DATA load, CHART/RANKING quiet vs active | account/global shared limiter |
| Concurrency | same rate and paths at concurrency 1 vs 2 or 4 | in-flight/concurrency limiter |
| Minute shape | same calls evenly spread vs front-loaded, repeated across minute boundary | minute quota |

For every block, preserve raw request-level evidence and align counts to the server `Date`
second. Stop at the first unexplained 429; do not continue climbing. Repeat only clean
points enough times to separate a threshold from a transient failure.

Interpretation is pre-declared:

- failure by MARKET_DATA count regardless of path => hidden MARKET_DATA bucket;
- failure only for one path => path limiter;
- failure threshold moves with other groups => account/global limiter;
- failure moves with concurrency but not rate => concurrency limiter;
- failures persist to a minute boundary and track cumulative minute calls => minute quota.

Firing either stage, changing any ceiling, or deploying any probe is outside this task.

## 7. Reproduction and gate

The existing runner reproduction is read-only and opens only exploration B:

```text
.venv\Scripts\python.exe -m tossmon.analysis.measure.candle_ladder \
  C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\data\tossmon.db \
  --era B --out out\budget_ceiling --name candle_ladder_era_b
```

Seat sweeps reuse the runner's 79 events and `placebo_candidate_mask`; the interval method
is specified in sec. 3.1. Generated `out/` files are ignored and are not part of this PR.

Required repository gate:

```text
.venv\Scripts\python.exe -m pytest tests/ -q
```

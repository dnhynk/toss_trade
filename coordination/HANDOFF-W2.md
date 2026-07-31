# HANDOFF — W2 (store/universe owner)

- **ID / role**: W2 — owns `tossmon/store/**`, `tossmon/universe/**`,
  `tests/test_store*.py`, `tests/test_universe*.py`.
- **Branch**: `w2-universe-store`
- **Worktree path**: `C:\Users\dongh\orca\workspaces\toss_trade\w2-universe-store`
- **Current HEAD**: `15022a27be52e9de72ce00455328ad6fb677e1d7`
  (`fix: add build_universe entry point, fix audit F-2 (no caller)`)

## Working tree state

**Clean. Nothing uncommitted.** All W2 work for this session is committed and
already merged into `main` (verified: `git merge-base --is-ancestor <commit> main`
returned true for all three below). No new HANDOFF-triggered commit was needed.

My branch `w2-universe-store` is `0 ahead / 7 behind main` right now — it has not
been rebased onto the latest main since my last commit, but there is nothing local
to lose (HEAD is a strict ancestor of main).

## What I was asked to do, across the session (my own summary)

1. **Contract A3** — `orderbook_snap.imbalance` was stored signed `(bid-ask)/total`
   `[-1,1]` but the contract doc said ratio `[0,1]`. Coordinator decided: signed is
   canonical, rename the column to `imbalance_signed` so it self-documents.
2. **events dedup bug (live-observed)** — detector rescans its buffer every cycle
   and re-detects the same event; `events` had no uniqueness constraint, so
   duplicate rows inflated evaluate.py's q1–q6 stats (~40% inflation measured live).
   Fix: schema v2 migration (dedupe to max-id row per `(symbol, t0_ms)`, preserving
   whatever was already live, then add a UNIQUE index) + `record_event` upsert
   instead of insert-or-ignore (later redetections have more complete labels).
3. **Audit F-2 (build_universe has no caller)** — two independent adversarial
   audits found `build_universe` was never invoked anywhere, so the Tier-0 filter
   never ran on the collector's watchlist and megacaps (NOK/AMD/ASML/META/GS)
   filled tier2/blocked tier3. Added `tossmon/universe/__main__.py` as the missing
   entry point (`python -m tossmon.universe --config ...`, symmetric with
   collector's), documented the `Reader.symbols()` seed contract for W4's
   collector-side consumption, added idempotency/partial-failure/cache-fallback/
   entry-point-smoke tests.

## Done (commit hashes, all merged into main)

- `2998a54` — `orderbook_snap.imbalance` → `imbalance_signed` (contract C-6 A3).
  Merged as `b6a5616` on main.
- `aca1be8` — events dedup: schema v2 migration + upsert `record_event`.
  Merged as `f4d11e2` on main (`Merge W2: dedup events with UNIQUE(symbol,t0_ms)
  and preserving migration`).
- `15022a2` — `tossmon/universe/__main__.py` entry point + `Reader.symbols()`
  seed-contract docstring + tests. Merged as `3ca67a3` on main
  (`Merge W2: build_universe entrypoint (audit F-2 supply side)`).

**No open/uncommitted W2 work exists right now.**

## Next task / resume point

**None assigned yet.** I was idle, waiting for the coordinator to re-engage after
this orchestration reset. When re-engaged with a new task, treat it as fresh —
the previous taskId/dispatchId (`task_9d07db149b14` / `ctx_e52dc5b4767d`, the
build_universe task) are stale per the reset notice and should not be reused.

One thing the coordinator flagged as still outstanding **before** the reset
(not urgent from my side, just noting it hasn't happened yet): the live DB
migration for the events-dedup schema v2 change was deliberately **not** applied
by me — the coordinator said they'd apply/instruct it themselves after tonight's
W5 live collection session ends. If a fresh W2 gets re-engaged and that hasn't
happened yet, that's still their call to make, not something to do unprompted.

## Blocked / awaiting coordinator

Nothing currently blocked. No open questions pending an answer.

## Things the next person (or a fresh me) should know

- **Local venv was missing dev deps** (`filelock`, `httpx`, `pandas`, `pyarrow`,
  `pytest-asyncio`, ...) for most of this session, which made `python -m pytest`
  fail to *collect* several test modules (`ModuleNotFoundError: No module named
  'filelock'`, plus a separate `tests.test_collector_helpers` rootdir import
  error that reproduces identically on a clean `main` via `git stash` — i.e. not
  caused by my changes). **Root cause turned out to be running the wrong
  interpreter**: plain `python` on PATH is *not* this worktree's venv. Use:
  ```
  .venv/Scripts/python.exe -m pytest -q
  ```
  Once I did that, `pip install -e ".[dev]"` reported everything already
  satisfied and the full suite ran clean (641 passed at the time, on a slightly
  older main — the coordinator later reported 694 on newer main / 616 after
  the events-dedup merge / 653 after W4's blocker-A/B merge). **Don't trust a
  bare `python -m pytest` failure in this repo without first checking you're
  using `.venv/Scripts/python.exe`.**
- The full suite takes ~150s — it will exceed a 120s foreground bash timeout
  and get backgrounded. That's expected, not a hang.
- `git rebase main` has worked cleanly every time I've done it this session
  (no conflicts) — my owned paths (`tossmon/store/**`, `tossmon/universe/**`)
  haven't been touched by other workers' merges so far. W4's most recent merge
  (`da19527`, dedup-key change in the collector's detector) only touched
  `tossmon/collector/**` and its own tests — no overlap with my files, no
  conflict expected on next rebase.
- Ownership boundary reminder for whoever picks this up: `tossmon/collector/**`
  and `tossmon/config.py` are **not** mine (W4-owned) — I only *import* from
  them (e.g. `TossClient`, `TokenManager`, `load_config`) in
  `tossmon/universe/__main__.py`, never edit them.
- Contract for `Reader.symbols(tier=...)` is now pinned in its docstring
  (`tossmon/store/reader.py`) — read that before changing anything about how
  the collector seeds its watchlist from the `symbols` table; W4's blocker-A
  fix already consumes it (`ctx.watch()` reads `symbols` per that contract).
- **Never issue live tokens / call the live API** — this rule held for the
  whole session (mock-only, `TOSS_LIVE=0` / `TOSS_BASE_URL=http://127.0.0.1:8899`
  or the ephemeral `mock_server` pytest fixture from `tests/test_api_support.py`)
  and still holds post-reset per the coordinator's notice.

## Test status at last check (this session, before the reset notice)

- `.venv/Scripts/python.exe -m pytest -q` → **641 passed**, 0 failed, 0 skipped,
  on main `79fec4c` / my HEAD `15022a2`.
- Coordinator's own re-runs after merging each of my three commits (on newer
  main, with deps already installed) reported 455 / 616 / 641 passed
  respectively at each merge point — all green, no regressions attributed to
  my changes.
- Coordinator's broadcast states current `main = 4869293` is **694 tests
  green** as of the reset notice.

# operations — running the council for real 🧮

Phase 7c of the rooms overhaul ([`ROOMS_DESIGN.md`](ROOMS_DESIGN.md) §10):
the multi-user *invariants* (tenant scoping, revocable device credentials,
scoped websockets, the sync quota) landed in phases 1–2 — this phase is the
operator's side of running them: metering, budgets, backups, load, and the
distribution gatekeeping. Built in slices; each section below says whether
its piece exists yet.

## the meter (built)

Every model call has been a `usage_log` row since v3 (user, helper, role,
model, four token classes). The meter is the first thing that reads it:
an operator dashboard, and the per-user token budgets §10 promised.

### the dashboard

Set `OPS_TOKEN` (any long random string — `openssl rand -hex 24`) in the
server's env and `/ops` exists; unset, the routes don't (same pattern as
the update feed). Open `https://api.internetcreature.dev/ops`, paste the
token once per tab (sessionStorage only), and you get: per-day, per-user,
per-model, and per-role token aggregates over a 7/14/30-day window, plus
each user's live trailing-24h spend against the budget knobs — the same
arithmetic the gates enforce, so the dashboard can never disagree with
the behavior. Days are UTC (an operator view spanning users has no one
"local"); budgets are trailing-24h.

The page is inert html; the data endpoint (`/api/ops/usage`) is the thing
the bearer token guards, in constant time. The token is an operator
credential — don't reuse a user-facing secret for it.

### the budgets

Two knobs, both **off by default** (`0`) — a single-user rig stays
unmetered and byte-identical:

| knob | past it, what changes |
|---|---|
| `USER_TOKEN_BUDGET_SOFT_24H` | proactive outreach stops (a pulse gate, last in the stack). The user notices nothing except quiet. |
| `USER_TOKEN_BUDGET_HARD_24H` | conversational turns are refused with honest ephemeral copy ("resting its voice"). Clocks, sync, the deer, and every non-model surface keep working. |

Spend is measured over a **trailing 24h window** (the same rolling shape
as `SYNC_DAILY_EVENT_CAP`): no midnight cliff to time a burst against,
and the voice comes back gradually rather than at a bell. What counts:

- **Only the turn-driven roles** (conversation, scheduled outreach, and
  the decider that rides a turn) — the spend the gates can actually
  stop. Background work (curator, scorer, reconciler) is the system's
  own cost; counting it would let spend the user can't influence push
  them over the hard cap and hold them there. The budgeted set is an
  allowlist, so a future background role is exempt by default.
- **Input + output tokens, cache traffic never** — with openai's
  in-band cache hits normalized out (openai reports cached tokens
  *inside* `input_tokens`; anthropic keeps them in a separate field).
  A budget that punished cache hits would punish exactly the
  architecture that keeps the council affordable.

Honesty notes:

- Every enforcement point **fails open**: a broken meter never silences a
  check-in or a conversation (it logs and allows).
- The hard check runs **inside the per-user turn lock**: turns are
  serialized, so each check sees the spend the previous turn just
  recorded — a burst of simultaneous messages can't all pass on one
  stale read.
- The refusal copy is ephemeral — never persisted, never in the cached
  prefix.
- Sensible shapes: `hard` comfortably above `soft` (soft is the quiet
  lever, hard is the stop). Starting guesses for a real deployment:
  soft 300k, hard 1M — tune off the dashboard's own numbers.

## the vault (existing, pointers)

- **Postgres**: `scripts/pg_backup.sh` is cron-ready (nightly pg_dump,
  custom format, keeps newest 14). Restore rehearsal:
  `createdb restore_test && pg_restore -d restore_test backups/chordial-<ts>.dump`.
- **The updater signing key** (`~/.tauri/chordial-updater.key`) is the
  single unrecoverable secret — see
  [`PACKAGING.md`](PACKAGING.md); the update feed itself is reproducible
  from builds and needs no backup.
- **Sidecar sqlite** lives on each user's device and syncs derived events
  up; it is deliberately not the server's to back up.

## the drill (built)

`scripts/drill.py` load-tests everything a real fleet does — linking
devices, pushing outbox batches, holding websockets, taking turns —
against a real `WebService` over real TCP. Turns run through the **real
`ChatService` gateway** (per-user turn lock, budget seam, front door,
daily-room resolution); only the orchestrator is stubbed (real EventLog
writes, real AppInterface fan-out, zero tokens). It reports
client-observed p50/p95/p99 latencies per phase and *asserts* the
contracts under load: ACK cursors exactly match what was pushed, every
turn's reply arrives on the sender's **own** websocket and never anyone
else's (tenant isolation is checked for the whole run, not assumed),
the sync quota 429s honestly at the cap, and a retry of an already-ACKed
batch still ACKs at the cap (dedup-before-quota).

```
python scripts/drill.py run                    # 25 users, defaults
python scripts/drill.py run --users 100 --turns 5
python scripts/drill.py run --db-url postgresql+psycopg://localhost/chordial_drill
```

Safe by construction: it refuses any database whose **name** doesn't
contain `drill` (the name specifically — a password or query param
containing the word doesn't count), defaults to a throwaway sqlite file
it deletes afterwards (`--keep` to inspect), and spawns its own server
subprocess — it never speaks to a real deployment.

### the numbers (2026-08-20, M-series laptop, localhost)

All correctness checks pass at every scale tried, on both engines.

- **100 users, sqlite (WAL)**: zero errors. Turns through the full
  gateway: 226 POSTs/s, p50 411ms under 100-way concurrency; websocket
  delivery p50 342ms; 100 sockets held. Sync-push throughput pins at
  **~27 pushes/s (~2,700 events/s)** — sqlite's single writer — so a
  full-fleet outbox stampede queues: p50 3.3s per push while all 100
  devices flush at once, clearing in ~7s total.
- **50 users, postgres (localhost)**: same shape, push ceiling **~16/s
  (~1,600 events/s)** — each event costs a savepoint round-trip by
  design (the race-safe landing). Turns 120/s, p50 375ms.
- Reading it honestly: the stampede is the worst case, not the steady
  state. Real sidecars emit a few derived events per minute; even the
  cap (5,000/day/user) is two minutes of drill traffic. At today's
  target scale the current shape has headroom; revisit past a few
  hundred users, and prefer postgres for any real multi-user
  deployment (concurrency, backups, no single-writer cliff).

### the tuning that fell out

- **Fresh postgres installs were broken** — found on the drill's first
  postgres run, fixed in this slice. A boolean column default rendered
  as integer `0` (invalid postgres DDL) broke *both* install paths
  (`init_fresh_db.py`'s create_all and `alembic upgrade head`), and one
  migration used a 68-char constraint name (postgres caps identifiers
  at 63). `tests/test_postgres_portability.py` now compiles the whole
  schema under the postgres dialect so the class stays dead. The full
  migration chain was applied and round-tripped on scratch postgres.
- **`LINK_RATE_ATTEMPTS` / `LINK_RATE_WINDOW_SECONDS`** (default
  10/300s): the code-redemption limiter is per client ip, so many
  legitimate devices behind one NAT ip (a classroom) need the window
  widened without a code change.

## the seam (built)

Chordial consumes [the-dainframe](https://github.com/internet-creature/the-dainframe)
as a **path dependency** (`../the-dainframe`, `develop = true`) — instant
cross-repo iteration, no publish step. The cost is that the lockfile pins
every dependency by hash *except* this one, so the seam has its own
operating rules:

- **Deploying is a two-repo pull *plus the install*.** The server needs
  both clones side by side; after merging coupled work, `git pull` in
  **both** — a chordial-only pull can import symbols its dainframe
  doesn't have yet (loud, an `ImportError` at boot) or run against
  changed semantics (quiet, worse). Then `poetry install` in chordial
  before restarting: pulling source updates neither the locked
  third-party set (a release that adds a dependency fails at boot
  without it) nor the installed dainframe metadata the startup stamp
  reports — a pull-only deploy shows the new SHA beside a stale version.
- **Merge order: dainframe first.** A chordial PR that leans on new
  dainframe API must land *after* its dainframe PR — chordial's main
  should never require a dainframe branch.
- **Releases are tags.** The dainframe tags consumer-facing API changes
  (`v0.1.0` = the cadence release); the tag annotation carries the
  changelog. Version numbers name what prod runs — nothing is published
  to an index.
- **The process says what it's running.** The first startup log lines
  include `dainframe <version> @ <sha> from <path>` — when prod behaves
  oddly, read the combination off the log instead of asking both clones.
- **CI tests the shipping pair.** Both repos run their suites in GitHub
  Actions; chordial's workflow checks out the dainframe's **main**
  beside it — the combination that ships, which a dev machine sitting
  on feature branches structurally never tests.

## the gate (decision pending)

macOS code-signing + notarization for the boxed app (the 7b deferral:
the .app runs locally unsigned; gatekeeper will refuse it on anyone
else's machine). Needs an Apple Developer Program membership ($99/yr) —
an operator decision, not a code one. Until then distribution is
"build it yourself" per [`PACKAGING.md`](PACKAGING.md).

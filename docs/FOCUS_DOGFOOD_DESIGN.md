# the first dogfood round: the deer window, task scope, and vel's sense of the day

*status: designed 2026-09-02 from Megan's dogfood feedback on the boxed app. decisions
below are hers. not yet built. the willowden design doc (portfolio-site/wiki/willowden)
was revised the same day to carry all of it — §8 lists where. §10–§12 were added in the
second pass: planning at the grain of a task, the companion window's forms and controls,
and the mvp build plan that supersedes §6.*

**naming:** the "deer window" is now the **companion window** everywhere (surface names:
Home / Companion window / Rooms / Collections). the tauri window label `deer` and the
component file names can stay until the willowden rename slice; user-facing copy and docs
say companion window from here on.

## 0. the feedback, and what the code actually does today

| feedback | what's there now |
|---|---|
| clicking a task should not auto-start it | `DeerWindow.onPickTask` calls the sidecar's `/v1/focus/start` on the row click itself. no selected state exists; a click while another clock runs is an instant switch. |
| no way to remove a task | the app's only task verbs are quick-add (`POST /api/v1/tasks`) and status (`POST /api/v1/tasks/{id}/status`). tasks never hard-delete (workspace convention); closed statuses are `done` / `deprioritized`. |
| a little scoping workflow when a task has no scope | `Task` has no scope field. `description` exists (free text, rendered nowhere in the app); `Commitment.next_action` is the "activation-energy field" pip already uses. the sidecar run carries a `label` (currently the bare title) which becomes pip's "landed X" observation. |
| vel needs the day's pomodoro context when reaching out | the scheduled-tick prompt is hardcoded to vel, byte-identical for desktop and telegram, and contains **zero** focus data: no running clock, no banked minutes, no blocks landed, no tasks finished today (the agenda's `done_today` is deliberately empty — "the wins ledger owns this now"). `focus_block.completed` becomes a pip observation (tool-only) and a presence event on the legacy stream that room prompts never replay. the instruction is four generic bullets ("ask something open-ended"). nothing makes a tick block-aware. |

### decisions locked 2026-09-02 (Megan)

1. **scope = one next action + a target.** a short line (pip's "one stupid little thing")
   plus a minute target. new `Task.next_action` column. it becomes the clock label and
   flows into pip's observation.
2. **scoping is an authored inline form, skip remembered.** the deer window asks once
   per task per day when a task has no scope; "just start" is always one tap away;
   skipping isn't re-asked that day. no model call, no chat detour.
3. **removal = set aside, then decide, with no time pressure.** a task can be set aside
   from its row; it moves to a greyed section at the bottom of the deer list, *below*
   the completed ones, and stays interactive there: push to tomorrow, let it go
   entirely, bring it back. nothing disappears on a timer.
4. **mid-block reach-outs are allowed but block-aware.** the digest tells vel a clock
   is live and the instruction shapes the message accordingly; she is never gated off
   by a running clock.

---

## 1. the deer window: select, then start

**selection is a UI state, not a sidecar one.** `selectedId` lives in `DeerWindow`.
clicking a row selects it (click again or click away to collapse). the selected row
expands into a small detail strip:

- the scope line if the task has one (`next_action`), otherwise the scoping form (§3)
- target chips: **10 · 25 · 50** minutes, default = the task's last-used target this
  session, else `pom_minutes`
- the primary button: **start** — or **switch to this** when another task's clock is
  running (banks the old run exactly as today, just explicitly), or nothing when this
  row *is* the running task (the run controls already live above the list)
- a quiet secondary action: **set aside** (§2)

the `▸` glyph on each row (already in the willowden mockup) is a shortcut: it starts
the task with its existing scope and last target without expanding. when the task has
no scope, `▸` expands the row into the scoping form instead — never a silent start.

keyboard: Enter starts the selected row; Escape collapses it.

the running task's row stays highlighted as today; the clock header renders the run
label (§3 defines it).

## 2. set aside: the parked section

**schema:** `Task.set_aside_on` (Date, nullable, user-local calendar date). set aside
is a *today* decision, not a lifecycle change: the task stays in an open status.

**today payload** (`_today_payload`) gains a bucket `set_aside`: open-status tasks
with `set_aside_on == today`, excluded from `today` / `overdue` / `in_progress`.
the deer list renders, top to bottom: open rows → done rows (✓, as now) → set-aside
rows (greyed, still clickable). `Home` gets a matching small group ("set aside").

**parked row actions** (the row expands on click like any other):

| action | server effect |
|---|---|
| tomorrow | `scheduled = today + 1`, `set_aside_on = null`, and `status = todo` if it was `in_progress` (otherwise the "in motion" bucket would keep showing it) |
| bring back | `set_aside_on = null` |
| let it go | `status = deprioritized` (closes it; `closed_at` stamped by `_apply_status`), `set_aside_on = null` |

setting aside the **running** task pauses the sidecar clock first (client-side, banks
the run), then patches the server — the v1 status/patch routes never touch the
sidecar clock, so the window owns that ordering.

**rollover (assumption, flagged for Megan):** at the next local day `set_aside_on`
no longer equals today, so the row leaves the parked section. its `scheduled` date
is unchanged, so it reappears as *carried over* — the quiet default return, with no
nag attached. the alternative (setting aside also unschedules) would make it vanish
from the app until the council re-plans it. default is the quiet return.

**council awareness:** the agenda digest stops listing set-aside tasks under
`today (N)` and adds one line `set aside today: "x", "y"` — so vel and pip don't
nudge about a task the person consciously parked.

## 3. scope: next action + target

**schema:** `Task.next_action` (String, nullable, cap 140 chars — same discipline as
`Commitment.next_action`). `description` keeps its long-form role.

**the scoping form** appears inside the expanded row when `next_action` is empty and
the person hits start/▸:

> **what's the first piece?** `[ one line… ]`  `10 · 25 · 50`  **[start]**  *just start*

- "start" saves the line (PATCH) and starts the clock with it.
- "just start" starts without a scope and remembers the skip for this task for today
  (`localStorage` key `chordial.scope_skipped:<task_id>:<yyyy-mm-dd>`; per-viewer
  convenience, never authoritative). the next start of that task today goes straight
  through; tomorrow it asks again.
- the form is authored copy, deer-voiced, and Megan's voice pass may replace the
  strings — they live in one place in the component.

**the run label** (`/v1/focus/start` `label`): `title` when unscoped,
`"<title>: <next_action>"` when scoped (sidecar cap 200 applies). the window splits
on the first `": "` to render title above and scope beneath the clock (the willowden
mockup's `outline section two · target 25:00`). pip's `landed <label> - N min`
observation and the cycle view inherit the scope for free.

`session.started` grows a `target_minutes` field in its payload (sidecar
`FocusEngine.start`) — the day digest (§5) needs it to say "18 min in, target 25".

**editing an existing scope:** the scope line in the expanded row is click-to-edit
(same PATCH). estimates (`pom_estimate`) stay the council's — the chips set the run
target, not the estimate.

## 4. server: one new route

`PATCH /api/v1/tasks/{task_id}` (device-bearer auth, tenant-scoped like its
siblings), body = any subset of:

```json
{ "next_action": "outline section two" | null,
  "scheduled": "2026-09-03" | null,
  "status": "todo" | "in_progress" | "done" | "deprioritized",
  "set_aside": true | false }
```

`set_aside: true` stamps `set_aside_on = user_today`; `false` clears it. status goes
through `vocab.canonical_status`. unknown keys → 400. returns `_task_row(task)`, which
gains `next_action` and `set_aside_on`. the existing status route stays (the offline
finish ledger uses it); willowden's rust core calls this same route.

migration: one alembic revision adding both nullable columns. `WorkspaceStore.update_task`
allows `next_action` and `set_aside_on`; `create_task` accepts `next_action`.

## 5. vel's sense of the day

### 5.1 the day digest (read model)

`src/services/focus_day.py` — `snapshot(user_uuid) -> FocusDay` and
`digest(user_uuid) -> Optional[str]`. pure db reads, guarded like every ambient
part (failure = no block, never a broken prompt). sources, all already on the server:

- `DeviceEvent` rows for the user with `occurred_at` inside user-local today:
  `session.started` / `session.ended` (credited `seconds`, `reason`),
  `focus_block.completed`, `drift.detected` / `return.detected`, `rewind.applied`.
  ordered by `(device_id, seq)`; a device whose latest session event is a
  `session.started` has a clock running since `occurred_at`.
- tasks closed today with `status == done` (the same query `_today_payload` runs),
  and open tasks with `set_aside_on == today`.
- planned-today / overdue open tasks with zero banked seconds → "untouched".

rendered (caps: 6 task lines, titles quoted like the agenda):

```
today so far (from their desk - background awareness, they haven't seen this):
banked: 58 min in 3 runs - "portfolio case study" 42 min (2 runs) / "practice piano" 16 min
finished today: "email the landlord"
right now: clock running on "portfolio case study: outline section two" - 18 min in, target 25 (since 3:14pm)
untouched today: "write cover letter" / "call mom"
set aside today: "clean desk"
drifted twice mid-block, came back both times
```

variants of the `right now` line: `last run ended 47 min ago (paused "practice
piano" at 16 min)` / `no runs yet today`. lines with nothing to say are omitted; a
day with no events, no finishes and nothing planned renders nothing.

**where it slots:** `ChordialContext._compose_ambient`, daily branch, after the agenda
digest and before the taper posture — on **both** scheduled ticks and ordinary user
turns (she should know the day when you talk to her, not only when she reaches out).
cycle rooms are untouched.

**not a coach surface:** the digest carries minutes, runs, finishes, drifts. never
streaks, phases, scores, or assessments — the ROOMS_DESIGN §5 line ("no coach pressure
in ambient presence") holds for what the deer *sees* as much as what she says.

### 5.2 presence rides along

`ChordialStimulusFactory` already resolves presence (`active` / `idle` / `absent`)
to pick the target; it now also writes `extras["presence"]` and
`extras["target_platform"]`. `ChordialContext.enrich` appends one ambient line on
scheduled ticks:

- active → `they're at the desk right now; this lands in the app.`
- idle → `they're connected but idle at the desk (N min); this lands on <platform>.`
- absent → `they're away from the desk; this lands on <platform>, phone-sized.`

### 5.3 the block-aware instruction

`build_scheduled_request` replaces its four generic bullets with a **posture** chosen
deterministically server-side from the `FocusDay` snapshot (computed in `enrich`,
passed as `extras["checkin_posture"]`; the prompt builder only renders it). one
posture per tick:

| posture | when | instruction (draft — instruction copy, not vel's voice) |
|---|---|---|
| `mid_block` | a clock is running | they're mid-block on "<label>", N min in of a T-min target. this is a word from the doorway, not a check-in: at most one light line, nothing that needs answering, no clock narration. if nothing is worth saying, say nothing. |
| `untouched` | no runs today, planned tasks exist | nothing has started yet. offer ONE first block — the likeliest tiny piece from what's planned (the carried-over one, or the smallest) — as an invitation, one tap away in the deer window. don't list the day. |
| `between` | runs banked, clock idle ≥ 20 min | name what actually landed, specifically. offer the next piece, or a smaller one. never "how's it going" in the abstract. |
| `wrapped` | every planned task finished or set aside, or evening in their tz | settle the day: what landed, no next-thing pressure. |
| `quiet_day` | nothing planned, nothing banked | the old shape: brief, warm, open — but grounded in the day so far if there is one. |

cross-cutting lines kept from today's prompt: be aware of the time without always
stating it; reference recent conversation if relevant; keep it short. added: *the
numbers are context, not content — you're a companion who happened to notice, not a
dashboard.* the persona block and exemplars are untouched; vel's voice in authored
lines is Megan's separate pass and ports in on its own.

`mid_block` relies on the script line's `response="optional"` — verify at build that
the helper honors an empty reply as silence (no event recorded, nothing delivered).
an unanswered doorway line does count toward the cadence ladder like any proactive
message; the banked block that follows resets the ladder (presence action event), so
in practice it never climbs.

### 5.4 what this does not change

delivery on desktop still lands in the main window's room (the willowden doc's
"deer bubble if you're at the desk" is a willowden slice, not this round — see §9).
the cadence ladder, quiet hours, and taper are untouched.

---

## 6. build slices (in order)

**A · server spine** — migration (`next_action`, `set_aside_on`); store allows both;
`PATCH /api/v1/tasks/{id}`; `set_aside` bucket + new row fields in `_today_payload`;
agenda digest's set-aside line. tests: patch validation/tenant scoping, bucket
membership incl. the in_progress+tomorrow case, agenda exclusion.

**B · the window** — select-then-start, `▸` shortcut, target chips, scoping form
with skip memory, run-label convention, parked section + actions (pause-before-patch
for the running task), `Home` set-aside group. vitest for the pure helpers (skip-memory
key, label split, list ordering).

**C · sidecar** — `target_minutes` on `session.started`. one test.

**D · vel's day** — `focus_day.py` snapshot + digest; ambient slot; presence extras;
posture selection + instruction rendering. tests: fixture `DeviceEvent` sets per
posture, digest byte-shape, "no events = no block" (prompt bytes unchanged for a user
with no device — cache-safety for everyone else).

A → B (+C) → D. D is independent of B except the set-aside line.

## 7. tests that guard the promises

- a row click never calls `/v1/focus/start` (B).
- `▸` on an unscoped task opens the form; on a scoped one starts (B).
- set aside on the running task banks first, then patches (B).
- a set-aside task is absent from `today`/`overdue`/`in_progress` and present in
  `set_aside`; "tomorrow" on an in-progress task resets its status (A).
- the scheduled prompt for a user with no device events is byte-identical to today's
  except the posture block (D) — warm caches survive for the system blocks.
- `mid_block` posture is chosen iff a device's latest session event is a start (D).

## 8. willowden: where this lands in the design doc

- **the deer** — mockup: the expanded row (scope line, chips, start) and the parked
  section under the done rows; "her states" unchanged. the `42m ▸` row affordance
  now has a defined meaning (start with scope; expand when unscoped).
- **a day, and a cycle → "start a block"** — becomes "select, scope, start": the
  scoping form is the "shrink it" moment. `next_action` on tasks is what Home's
  pulsing "one next action" reads.
- **falling off, and coming back** — the stall-nudge copy ("shrink it?" / "not today —
  hold it for tomorrow?") maps 1:1 onto `set aside → tomorrow` and the `between` /
  `untouched` postures; say so.
- **architecture** — the rust core owns the clock and emits the same events
  (`session.started` incl. `target_minutes`); the day digest is a *cloud* read model
  over synced events, so the port doesn't move it. `PATCH /tasks/{id}` is the app's
  task-edit verb.
- **the deer / who is she** — unchanged; the scoping copy is "your deer's", vel-adjacent.
- the cut-list check: none of this adds a process, a model call on the device, or a
  coach surface.

## 9. open questions (defaults stated; none block the build)

1. **parked rollover** — default: quiet return as carried-over next day (§2). the
   alternative is unschedule-on-set-aside.
2. **desktop delivery** — should a `mid_block`/`between` line mirror into the deer
   bubble when the main window is hidden? willowden says yes; here it's a later slice.
3. **chip values** — 10 · 25 · 50 assumed; the first-run design's 3/5/10 day-zero
   picker is a different moment and stays separate.
4. **Home** — stays read-only for tasks (the deer owns the clock); it only gains the
   set-aside group.

---

## 10. planning at the grain of a task (ask pip)

The person who is overwhelmed by one big project won't go looking for a planning session;
nothing today invites them into one except curiosity. Cycle planning with Pip is the
standing ritual. This is the smaller door, and the den learns when to hold it open.

**Not at creation.** Quick-add stays the frictionless jot. Overwhelm shows up at *start*
and in the *evidence* a task leaves behind — those are the two moments.

### 10.1 the door (pull)

- The scoping form (§3) gets a third choice beside *start* and *just start*: **ask pip**.
  The same button lives in any expanded row.
- It opens the main window's Rooms on today's daily room with a **Pip-led turn already
  begun** about that task. Mechanism: a new stimulus kind `task_breakdown` (audience
  `pip`, `extras={"task_id", "user_id"}`), fired by `POST /api/v1/tasks/{id}/breakdown`
  from the companion window. `ChordialDirector.direct` routes it to Pip with
  `response="required"`; `ChordialContext.enrich` adds the task (title, description,
  estimate, reschedules, today's banked minutes, set-aside history) as an ambient block
  plus an instruction: *open by proposing the first piece; ask at most one question; write
  the outcome with your tools.*
- Outcome path: Pip's tools write `next_action` on the task (`update_task` gains
  `next_action`), and optionally create two or three child tasks scheduled today
  (`create_task` with `plan_id`/`goal_id` as fits). The companion window refreshes on the
  room's websocket state change, or on focus, and the row now has a scope and a start.
- Two minutes in the daily room, not a planning room. Never a breakdown without asking.

**If Pip isn't met yet:** the door still works. The breakdown stimulus carries
`meet_if_needed`: the director lets Vel open with a one-line introduction and hand to Pip
in the same turn (the existing `meet_guide` path), so the door is never dead for a fresh
user. This is the only place this round touches introductions; the onboarding overhaul is
Willowden's.

### 10.2 the nudge (push)

Server-side, deterministic, computed in the `FocusDay` snapshot (§5.1) and surfaced as
`needs_breakdown: true` on the task row in `_today_payload`:

| signal | threshold |
|---|---|
| rescheduled | `reschedules >= 2` (Pip: "a task moved three times is data") |
| set aside | set aside on ≥ 2 distinct days (needs a tiny `task_set_asides` ledger or a counter column `set_aside_count`; counter is enough) |
| initiation failure | scope skipped today AND ≥ 2 runs on it today each < 5 min credited |
| untouched | on the day's list (today/overdue) ≥ 2 consecutive days with zero banked seconds |

- The companion window renders one authored chip on the flagged row — register: *this one
  keeps waiting; want pip to shrink it?* — dismissible, remembered per task
  (`localStorage`, plus `breakdown_offer_dismissed_at` on the task so other devices agree),
  never repeated.
- Vel's scheduled tick gains a `stuck` posture (§5.3) when any flagged task exists and no
  clock is running: name the task, offer Pip, one line. Pip may speak instead if the
  speaking policy is extended to scheduled ticks; v1 keeps Vel as speaker and lets her
  name Pip.
- Limits: one offer per task, phrased as the plan's fault. No chip on unscoped tasks that
  show no signal.

## 11. the companion window's forms and controls

Dogfood finding: the single fixed window (270×500, always-on-top, no decorations) is right
for choosing a task and obtrusive once the block is running on a single screen. It also has
no minimize or close. Both are fixed by giving the window **forms** and **real controls**.

### 11.1 forms

| form | when | contents |
|---|---|---|
| **den** (full) | no clock running; choosing, scoping, tidying | deer + bubble, task rows with banks, done rows, set-aside rows, quick-add — the current layout |
| **bar** (focus) | a clock is running | one slim horizontal strip, ~420×56: deer glyph (carries watch/perked/hushed), task title, scope line, clock + fill, pause, finish. click anywhere else → den |
| **docked** | user chose "inside Home" | willowden only; not in the mvp |

- Start → den→bar by default (`companion.auto_bar`, default on, one toggle in the bar's
  overflow). Pause/finish → back to den. Clicking the bar opens the den *without* stopping
  the clock; the den offers "back to bar".
- Each form remembers its own position (the window-state plugin stores one rect per
  label, so per-form rects live in `localStorage` and are applied on switch via
  `setPosition`). Always-on-top stays on in both forms; a toggle lives in the overflow.
- The bar's lines are the same authored pools, shown as a one-line ticker for the linger
  time; celebrations still un-loaf for a moment (bar grows briefly, then settles).
- Rewind offers: the bar shows the chip form only; the card opens the den.

### 11.2 controls

Three small buttons in the den header and the bar's right edge, all keyboard-reachable:

- **minimize** → `hide()`; the tray's existing "show / hide the deer" brings her back.
- **close** → `hide()` as well (a hidden window never stops the clock — the sidecar owns
  it). The tray menu item is renamed "show / hide the companion".
- **always-on-top** toggle.

Tauri: the `deer` window needs `resizable: true` for the form switch (or two windows —
rejected: one webview, two layouts, `setSize`/`setPosition` on switch). Capabilities add
`core:window:allow-hide`, `allow-show`, `allow-set-size`, `allow-set-position`,
`allow-set-always-on-top`, `allow-set-focus`. The fixed-size assumption in `placement.rs`
(first-launch layout) uses the den rect.

## 12. the mvp build plan (supersedes §6)

Chordial is where dogfooding happens, so the basic functionality lands here first;
Willowden inherits it with the Rust core port. Ordered by dogfood value per day of work;
each slice is a PR, Sol reviews as usual.

| # | slice | scope | why this order |
|---|---|---|---|
| 1 | **the window, basics** | select-then-start (▸ + button), target chips, minimize/close/always-on-top controls, den→bar form with per-form positions | the two loudest complaints (auto-start, obtrusive) with zero server work; usable the same day |
| 2 | **server spine** | migration (`next_action`, `set_aside_on`, `set_aside_count`, `breakdown_offer_dismissed_at`), `PATCH /api/v1/tasks/{id}`, `set_aside` bucket + new row fields, agenda's set-aside line, `update_task` tool gains `next_action` | unblocks 3 and 4 |
| 3 | **scope + set aside** | scoping form with skip memory, run label convention, `target_minutes` on `session.started` (sidecar), set-aside section + tomorrow/bring back/let go, Home's set-aside group | the scoping workflow and removal, end to end |
| 4 | **vel's day** | `focus_day.py` snapshot + digest, ambient slot (ticks and turns), presence extras, posture instruction incl. `stuck`, `needs_breakdown` flags | the reach-out overhaul; independent of 1 and 3 except the set-aside line |
| 5 | **ask pip** | `POST /tasks/{id}/breakdown` → `task_breakdown` stimulus → Pip turn with task context + meet-if-needed; the chip on flagged rows; window refresh on outcome | needs 2 and 4 |

Estimated shape: 1 and 2 are each a day; 3 and 4 two days each; 5 a day plus a prompt
pass. Test promises from §7 carry over; slice 1 adds: a form switch never calls the sidecar;
hide never pauses; the bar's rect round-trips.

**Willowden reflection:** the wiki page already describes all of the above (revised
2026-09-02). When a slice lands in chordial and dogfood changes a detail, the wiki page is
updated in the same PR week, not later.

---

## 13. the morning, and the shape of a day

Dogfood 2026-09-03: no message all morning; the previous day's outreach landed at 15:19
and 21:55. Server-side reading (confirmed against the code): the only proactive beat is an
hourly `Interval` anchored on the last activity, so every reply restarts a one-hour clock
and nothing caps it per day; the `@ 8-11` window lives inside the cadence ladder and only
shapes *follow-ups* to an unanswered chain — the first send after any reply lands wherever
the hour falls; three quiet days escalate an otherwise-active user to weekly.

Likely mechanism for the silent morning (unverified against logs): the 21:55 send was
proactive and unanswered, so the ladder's first rung waited 1d → 21:55 next day → outside
8-11 → snapped to the morning *after* — the "rung wait + morning snap skips a day" nuance
from the cadence release. Either way the conclusion holds: **the day has no shape.** Vel
speaks when an hour happens to elapse, not when the person's day has a moment for her.

**Decisions 2026-09-03 (Megan):** keep the hourly follow-through, capped to once a day;
skip the brief when she has already messaged that morning (fold it into the conversation);
desktop "being seen" = OS notification only in the mvp.

### 13.1 the day gets beats

Replace "one hourly beat forever" with day-anchored beats, each its own `rhythm_id` (the
pulse store tracks horizons per key), all `kind="scheduled_tick"` so the existing gates
apply, with `FiringPlan.extras["beat"]` naming which one fired:

| beat | rhythm | when | posture (§5.3) |
|---|---|---|---|
| **morning brief** | `Calendar(cron, tz_of)` — exists in dainframe (`rhythms.py:75`, "the morning-brief shape"), `misfire="skip"` | user-local `morning_time`, default **08:30** (preference), inside delivery hours | `morning` (new, §13.2) |
| **follow-through** | the existing `Interval` beat, kept, but capped | at most **one** per local day, never within 3h of the brief, anchored on activity as now | `mid_block` / `between` / `untouched` / `stuck` |
| **evening settle** | `Calendar`, default **20:30** | only if today has runs, finishes, or a stated plan | `wrapped` |

The taper keeps stretching the follow-through beat; the brief and the settle are daily by
definition and taper by *skipping days* later (keeping-watch users get the brief on Mondays
only — a follow-on, not this round).

### 13.2 gates for a shaped day

Three small gates, chordial-side first (dainframe candidates once willowden wires the
same — the "presence within N days" and "N per local day" shapes are generic):

- **RecencyGate** (brief + settle only): deny unless a user-authored presence event
  (`message` or `action`) exists within `MORNING_RECENCY_DAYS = 4`. Beyond that the
  cadence ladder owns the person (weekly, then the 60-day floor). Reads events and
  compares `created_at` exactly like `CadenceGate` does — no `EventQuery` time fields
  needed for the mvp (a `since` field on `EventQuery` is the clean dainframe follow-up).
- **AlreadyTalkedToday** (brief only): skip the brief if the person messaged today
  before it — they're up and talking, and the `morning` posture rides that conversation's
  ambient instead (§13.2). Banking a block does *not* skip it; a person who started
  working in silence still gets the wrap-up and the first-thing nudge.
- **DayCapGate** (all beats): at most `PROACTIVE_DAILY_CAP = 3` proactive sends per
  user-local day, and no two beats within 3h. Counts agent messages with
  `message_type="scheduled"` since local midnight.

**The ladder governs the follow-through, not the brief** (found while building: the
ladder's rungs are 24-hour waits, so a check-in at 21:55 plus "1d" lands at 21:55 and
snaps past the morning window — under the ladder the brief would skip a day after any
evening send). The brief's ladder *is* the recency window. Unanswered briefs still count in
the chain — they are proactive sends — so ignored mornings hold the follow-through exactly
as ignored check-ins do, and after four quiet days the brief stops and the weekly rung
takes over. `PROACTIVE_DAILY_CAP` defaults to 2 until the evening settle lands.

### 13.3 the `morning` posture

Ambient for the brief adds **yesterday**: the `FocusDay` digest (§5.1) computed for the
previous local day (runs, banked, finished, set aside) plus the previous room's summary
that already rides as `previously:`. Then the instruction, one of two shapes chosen
deterministically from today's agenda:

- **no agenda for today** → *wrap yesterday in a line if there's anything to wrap, then
  ask what today's shape is — one open question, not a form. if they've been quiet a few
  days, that's fine and unremarkable.*
- **agenda exists** → *wrap yesterday in a line, then point at the one first thing: the
  carried-over task, the highest-priority today task, or the cycle commitment with a next
  action — in that order. propose it as one small block, one tap away in the companion
  window. don't list the day.*

Cross-cutting: no greeting that ages badly (a brief read at 2pm must still make sense —
"this morning" not "good morning"); the numbers are context, not content; never streaks or
scores. The persona and exemplars stay untouched (Megan's voice pass).

`set_preference` gains `morning_time` ("08:30", or "off") and the tool description says
what the brief is; `outreach_cadence` is unchanged.

### 13.4 being seen

"If the user isn't engaged by the outreach message, they may never stop by to see it."
Delivery is already presence-routed (absent → telegram if linked, else the app), and the
brief lands at a time most people are away from the desk. Three additions:

1. **OS notification** from the Tauri app when a proactive line arrives and the main
   window isn't focused (`tauri-plugin-notification`; mvp = one setting, on by default;
   willowden = the consent ladder's "native notifications" row). **Megan's call
   2026-09-03: this is the mvp's answer** — the companion bubble mirror ("deer bubble if
   you're at the desk") stays a willowden design item and is not built here.
2. **Unread at launch**: an unread brief renders first when Home opens, with its
   timestamp — the willowden "invitation prepared for the next launch".

### 13.5 build

Cheap and high-value, so it moves up the §12 order:

| # | slice | scope |
|---|---|---|
| 1 | window basics | (unchanged) — **BUILT 2026-09-03** on `dogfood/window-basics`: select-then-start (row click selects; ▸ / start / switch run the clock; Enter/Escape), 10·25·50 chips remembered per task per session, den ↔ bar forms with per-form positions and the auto-bar toggle, pin / minimize / close controls (both hide; tray restores). smoke-verified in the browser pane against a scratch sidecar; the tauri geometry calls are guarded no-ops outside the shell and need the boxed-app smoke |
| **2** | **the morning, v0** | `Calendar` brief rhythm + `morning_time` preference; RecencyGate, AlreadyTalkedToday, DayCapGate; `beat` in extras → `morning` posture from agenda + `previously:` only; follow-through cap. Server-only, ~a day. Tests: the 21:55-unanswered scenario produces a brief at 08:30 the next day; a reply at 07:50 skips the brief; day cap holds. |
| 3 | server spine | (was 2) |
| 4 | scope + set aside | (was 3) |
| 5 | vel's day | (was 4) — enriches the brief with yesterday's `FocusDay` digest |
| 6 | ask pip | (was 5) |
| 7 | being seen | OS notification + unread-at-launch; evening settle rhythm (bubble mirror deferred to willowden) |

**Willowden reflection:** the wiki's "a day, and a cycle" and "falling off" paragraphs
now describe the three beats, the day cap, and the being-seen path (revised 2026-09-03).

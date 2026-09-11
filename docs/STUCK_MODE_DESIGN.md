# stuck mode: the "i'm stuck" button, and the page behind it

*status: proposed 2026-09-09 from Megan's idea after a low-motivation day, plus the
GPT-6 Astra review of the codebase (its thesis: chordial cares for what happens after
a start and asks the most of the person right before one). decisions marked
**(Megan)** are hers; everything else is a proposal for her pass. not built. the visual
companion to this doc (mockups of every state) was published the same day; the
willowden wiki page gets a section when she says go. revised after implementation
review: rest removes the list without changing it, attention/return becomes its own
multi-signal seam, proposal effects happen only on acceptance, and the v0 boundary is
explicit.*

**the promise, in one line:** *when you care about something and can't get yourself
moving, press the button. that's the whole ask.*

## 0. what the product does today when someone is stuck

| the person | what's there now |
|---|---|
| can't choose between the tasks on the list | the companion window shows every task (in motion / today / carried over / parked). the morning brief points at one first thing, but only in the morning, and only if it was read. |
| can't start the task they've chosen | the scoping form (#87) asks *what's the first piece?* — a good question that hands the hardest part back. "ask pip" (FOCUS_DOGFOOD_DESIGN §10) is designed, not built, and it opens a chat. |
| is depleted, hungry, sleep-deprived, or has been gaming since noon | nothing. the drift detector watches idle time *inside* a block; the day before a block is invisible. `scorecard._late_seconds` knows about last night but nobody reads it before the evening. |
| wants help without explaining | every door is a conversation: the room, telegram, the scoping form. Pip's persona already knows the move (*"we are opening the draft and writing the first ugly sentence"*) but only says it when asked. |

Pressing one button and receiving one usable proposal is the case the product has no
answer for. It is also the case Megan hit on 2026-09-08.

### decisions locked 2026-09-09 (Megan)

1. **the model turn is the main path, and waiting for it is welcome.** a clear
   loading state with honest copy ("formulating a strategy for getting you unstuck")
   beats a fast generic answer. a fast-but-dumb proposal is what would make her fall
   off. the deterministic ladder (§6) is the *fallback*, shown only when the house
   can't be reached or the turn fails.
2. **it opens a whole new surface.** the task list is the opposite of what an
   overwhelmed person should see. the page shows one thing.
3. **the AI does as much as possible.** the design question for every proposal is
   *what is the smallest contribution that actually has to come from this person
   right now?*
4. **this could be the defining feature.** simple, marketable, magical if it works.
   design it as the headline, not as a door in a corner.

---

## 1. the button

**copy:** `i'm stuck` — lowercase, no question mark, no "help". it states a fact the
person can state even when they can't state anything else.

**where it lives (every entry fires the same episode, §5):**

- **companion window, den form:** full-width, directly under the bubble and above the
  task rows. blush-wash fill, ink text — the warmest object in the window, and the
  only one that isn't a task. present in every den state (loaf, watch, perked), never
  hidden by a running clock: being stuck mid-block is real and the drift path doesn't
  catch it.
- **companion window, bar form:** a quiet `stuck?` text control beside pause. the bar
  stays slim.
- **Home:** the same button in the header, right of the date line.
- **tray menu:** `i'm stuck` as the first item.
- **rooms and telegram:** typing *i'm stuck* (or `/stuck` on telegram) fires the same
  stimulus; the proposals render as text (§9, v1).
- **willowden:** a global hotkey, and the page as its own small always-on-top window.

**never:** a cooldown, a counter, or any copy that notices frequency ("again?").
being stuck twice in an hour is a Tuesday.

## 2. the page

A single column, the width of a paragraph, centered in the main window. Nothing else
of the app is visible — no nav, no cycle strip, no list. The deer sits at the top,
ears soft, the same sprite as the den's head. The companion window hides (den) or
stays (bar, if a clock is running — the clock is never touched by opening the page).

### 2.1 thinking

The first thing shown, and shown deliberately:

> **formulating a strategy for getting you unstuck**
> *reading the day so far…*

- a breathing dot, not a spinner: four seconds in, four out (a regulation cue that
  costs nothing; `prefers-reduced-motion` gets a still dot).
- the sub-line rotates through authored lines every ~3s: *reading the day so far…* /
  *looking at what's on the list…* / *finding the smallest thing…* / *checking what
  worked last time…* / *almost.* The lines are honest about what the turn is doing
  (the brief in §5.2 really does read those things).
- target 5–15s. at 30s the fallback ladder (§6) appears with its own copy: *i
  couldn't reach the house, so here's the plainest thing i've got.* in v0 the
  fallback is terminal: a late model turn is stored for diagnostics but never swaps
  a live card under the person's hand. a later version may replace an untouched
  fallback, but only with an episode generation/version check and never after any
  reaction.
- no cancel button. closing the window is the cancel; the episode records it.

### 2.2 the card

One proposal. Fields, top to bottom (a proposal never has more than these):

| field | example | rule |
|---|---|---|
| **kind** (mono eyebrow) | `one small step` | one of §3's kinds; tells the person what sort of help this is before they read it |
| **the line** (serif, large) | *open the stuck-mode doc and write one sentence the deer could say. it can be bad.* | one sentence, concrete, a verb the body can do. never "work on", never "think about" |
| **the thing** (chip) | `willowden · stuck-mode design` | the task/plan it belongs to; in willowden, the file or app it opens |
| **enough** | *enough = one sentence, any sentence* | a boundary a tired person can recognise without judgment. every step-kind proposal has one |
| **container** (chip) | `2 min · then you choose` | optional. 2 is the default for a step; body/rest kinds carry their own (8 min walk) |
| **the why** (soft, small) | *you wanted the next stuck afternoon to have a door. this sentence is the door.* | from `Plan.why`, rewritten to the size of the step. hideable (§2.5) |

Then the controls, in this order and weight:

> **[ do this ▸ ]**
> *something different · that's too much*

The primary is the only filled button on the page. The other two are text.

### 2.3 something different

Rotates the **kind** of help, never the size of the same task. The turn returns three
proposals of three different kinds (§5.3), so the first two rotations are instant —
the card flips, the eyebrow changes, nothing loads. The third press sends a second
turn carrying the rejections (*not a step, not the body, not rest*) and the thinking
state returns with a different first line: *okay. different angle.* A fourth
rotation is allowed; a fifth shows the rest branch (§2.4) as the honest end of the
ladder, with the door back (§2.6) still open.

### 2.4 that's too much

The rest branch. No card. The page has already done the important thing: the task
list is gone. The deer makes that visual relief explicit without changing the day:

> *okay. you don't have to look at the list right now.*
> *nothing changed. it can wait outside this room.*
> **[ close ]**   *actually, one small thing*

Entering this branch performs **no workspace or contact mutation**. Tasks keep their
schedule and status; a running clock keeps its existing state; Vel's cadence and the
day cap are untouched. `close` hides/closes the stuck surface. *actually, one small
thing* returns to the proposal card with no undo work required.

This is deliberate: *that's too much* means "take the list out of my field of view,"
not "rewrite my plan" and not "silence the house." Setting tasks aside remains an
explicit task action in the ordinary companion window. If dogfooding later reveals a
desire for a true *hold today* action, it gets its own truthfully labelled control and
is not smuggled behind this branch.

One optional second line, only on evidence: past quiet hours, or `_late_seconds` from
last night above a floor → *sleep is the move. the list will be exactly where you
left it.* Otherwise nothing. Rest is never converted into tomorrow's productivity in
the copy.

### 2.5 the why, softened

The why line is on by default and hideable per person (`hide the why` under the card,
remembered — a preference, not a per-episode toggle). Some days "this matters" is the
help; some days it's the pressure. The turn also chooses between two registers for
the line from the situation: *this matters* (default) and *today's rough attempt
doesn't have to carry the whole thing* (chosen when the same plan's tasks show
set-asides or reschedules this cycle — caring is what's making it heavy).

### 2.6 the door back

Every state has a way to the previous one. The rest branch has *actually, one small
thing*; the card has the deer's head as a click target back to thinking (a fresh
turn); the fallback has *try the house again*. The page is never a dead end and never
a funnel.

## 3. kinds of help

The rotation set. The turn picks the first by the situation and must return three
distinct kinds.

| kind | eyebrow | what it is | typical when |
|---|---|---|---|
| step | `one small step` | one concrete entry action on a task already on the list | default; a task with `needs_breakdown` or a next_action nobody started |
| switch | `a different thing` | a provisional choice of a *different* task, made from priorities, with the reason in one clause | "can't choose"; every task on the list is untouched |
| thread | `pick up the thread` | restore the last context: what the last run was on, the unfinished decision, where the draft stopped | a run ended mid-way today or yesterday; the last room summary names an open question |
| body | `body first` | water, food, light, eight minutes outside, stretch — one of them, chosen by evidence | hours since the last run, meal-time, no runs since morning |
| sound | `something to hear` | music or ambient noise (points at it in mvp; opens it in willowden) | "can't start in silence" learned from episodes |
| rest | `rest` | stop, nap, early night, or simply close the list without changing it | late-hours evidence; past quiet hours; the third "too much" |
| company | `company` | *just sit with me*: a five-minute unnamed block, or the daily room with Vel already talking | the person keeps rotating; nothing else lands |

Every kind is a legitimate outcome. The page never implies that the step was the real
goal and the rest were consolation.

## 4. after "do this"

**step / switch / thread:** accepting claims the proposal first. Only then is the
prepared step written to `Task.next_action`, and only then does the page close and the
companion window appear as the bar with the run label `"<title>: <line>"` and the
container as target (2 min unless the chip was changed). Proposal generation itself
never edits a task. The run carries the episode UUID in `session.started` so the
outcome (§7) is attributable across devices.

**the boundary.** At the target the bar shows the exit — two exits, equal weight, an
authored line pool `stuck_boundary`:

> *that's the thing. stop here, or keep going — both count.*
> **[ stop here ✓ ]  [ keep going ]**

- **stop here** → the stop gets its *own* celebration line (`stuck_stopped`: *stopped
  at the line you drew. that's the whole skill.*) and the resume point is saved: the
  turn (or, when the model is out of reach, a template) rewrites `next_action` to
  what comes next, and the bubble says the evidence line: *you have the sentence now.
  next time doesn't start from blank.*
- **keep going** → an ordinary block; the target chip offers 10 / 25. no bonus
  language. (the `block_target` pool's "bonus minutes" / "overtime is yours" lines are
  flagged for Megan's voice pass — three of four lean toward continuing.)

A stuck-started run is persisted with `run_mode="stuck"` and its episode UUID. At
target crossing the sidecar emits this boundary instead of the ordinary
`block_target` announcement, never both. Repeated clicks and retries are idempotent;
the same accepted proposal cannot start a second run.

**body:** when the proposal actually involves leaving the desk, accepting it pauses
any running clock and opens a durable `AwayEpisode`: a small count-up (not a bank)
plus the prepared work step waiting underneath. On a real away → returned transition,
the bar re-offers that step: *back? the sentence is still ready.* A body action that
doesn't imply leaving (drink the water already beside you; stretch in the chair) does
not fake an away episode—it returns directly to the prepared step.

**sound:** points at a concrete sound choice in v0, then immediately re-offers the
prepared step. Sound is not treated as "away" merely because it is not work. Opening
playlists belongs to Willowden's later preparation privileges.

**rest:** closes or hides the stuck surface. It does not park tasks, pause a clock,
or suppress contact unless the proposal explicitly says to pause the current block
and the person accepts that typed action.

**company:** the unnamed block starts (label `just sitting`); at five minutes the
boundary exit appears as above, with the step re-offered as "keep going".

### 4.1 attention state: away, drift, and return

The current drift detector is the wrong primitive for the return bridge: it is armed
only by a running block, while a body proposal commonly pauses that block. Stuck mode
introduces an attention-state seam beneath both features rather than pretending every
absence is drift.

`AttentionState` consumes small, privacy-preserving signals and emits deterministic
transitions (`present`, `possibly_away`, `away`, `returned`):

1. OS input idle seconds and sample freshness (already collected)
2. explicit intent: an accepted body/away proposal and its episode UUID
3. focus state: running, paused, or idle, plus elapsed time
4. screen lock / unlock and sleep / wake when the shell exposes them
5. app focus and, only with the person's opt-in mappings, a derived frontmost-app
   category such as `work`, `meeting`, or `leisure` — raw bundle ids stay local
6. current surface visibility, so returning to the stuck card is evidence too

Rules stay legible rather than becoming an opaque score. Lock/sleep is hard away;
unlock/wake or fresh input is hard return. During an explicit `AwayEpisode`, sustained
idle can establish departure quickly. During a running block, the existing longer
threshold establishes drift. A frontmost-app category can strengthen context but is
never sufficient by itself to call someone away or distracted. Transitions use
hysteresis/debounce so a mouse twitch does not manufacture a return.

In v0, the seam ships with the signals already available: input idle, sample
freshness, focus state, explicit away intent, and surface visibility. Screen state and
opt-in app categories plug into the same state machine later. `DriftWatch` remains a
consumer that creates drift/rewind events only for running blocks;
`AwayEpisodeWatch` independently creates the one return event stuck mode needs. Both
survive a sidecar restart from durable state.

## 5. the episode, server-side

### 5.1 routes

```
POST /api/v1/stuck                {surface, task_id?, request_id} → 201 {ok, episode, replayed}
GET  /api/v1/stuck/{id}                                         → {ok, episode}
POST /api/v1/stuck/{id}/react     {proposal_id?, generation,
                                   reaction, request_id}         → {ok, episode, outcome, execution?}
```

*(as built, slice 1: the house `ok` envelope like every other v1 route; `episode`
= `{episode_id, status, generation, surface, source (model|fallback), proposals
(the current generation's three), rejected_proposal_ids, rejected_kinds,
exhausted, accepted_proposal_id, execution?, opened_at, ready_at, closed_at}`;
`outcome` ∈ `replay | recorded | regenerate | exhausted | accepted | rested |
closed`. a replayed open returns 200. `exhausted: true` on a generation-2 card
whose three were all turned down is the client's cue to show the rest branch;
the card itself stays. the sidecar handoff (`execution`) is
`{execution_id, episode_id, action, kind, task_id?, next_action?, minutes?, line}`.)*

`reaction` ∈ `accepted | different | too_much | closed`. The app polls `GET` every
1.5s while thinking (the room websocket is a chat surface; the page isn't one).
`request_id` makes create and react safe to retry, and `generation` prevents a late
turn from acting on a replaced card. The GET returns the hidden rotation set so the
first two `different` reactions remain instant; the client renders exactly one.

On `accepted`, the server validates that the proposal still belongs to this open
episode and applies any server-side preparation (such as `next_action`) in the same
transaction that claims it. The response carries a typed `execution` for the local
sidecar. The sidecar also deduplicates that execution id before starting or pausing a
run. A local failure leaves an accepted-but-not-started outcome that the page can
retry; it never silently creates a second block.

The legal state transitions are explicit: `thinking → ready | fallback | failed`;
`ready → thinking` only when a new generation is owed; and `ready | fallback →
accepted | rested | closed`. Terminal states reject later model writes. Closing is
best-effort from the client; a server sweep closes abandoned open episodes after a
short TTL, so correctness never depends on an unload request arriving.

### 5.2 the stimulus and the brief

`POST /api/v1/stuck` fires `Stimulus(kind="stuck", audience="pip",
extras={"episode_id", "task_id"?, "surface"})`. `ChordialDirector.direct` routes it
to Pip with `response="required"`. This is Pip acting as the house's background focus
specialist, not a social introduction: it works regardless of whether Pip has been
met, does not change helper state, and produces the deer's card register rather than a
Pip chat message. `ChordialContext.enrich` adds one ambient block, the **stuck
brief**, in this order:

1. the `FocusDay` digest (dogfood §5.1 — *this is why stuck mode waits for slice 5*)
2. today/overdue tasks with `next_action`, `pom_estimate`, `needs_breakdown`,
   `set_aside_count`, reschedules; plans named on those tasks with `why` (and zone
   once the covenant schema exists)
3. the clock: running/paused/idle, minutes since the last run, minutes since the last
   finish
4. time of day in their tz, quiet hours, last night's late seconds
5. the last five stuck episodes: kind accepted, outcome, the line that worked
6. `search_memories` results for the person's stated helps and hinderances (the
   persona already saves these as they come up)
7. the previous daily room summary (`previously:` already rides)

The instruction (draft; not Pip's voice):

> they pressed *i'm stuck*. that is the whole message: their capacity to direct
> themselves is low and they are asking you to take the initiative. propose THREE
> things of three different kinds, the first being the one you'd bet on for this
> exact afternoon. each is one concrete sentence a body can do, with what counts as
> enough. never ask a question. never offer a list to choose from. never "which one".
> never shrink the same task three times — change the kind. the smallest contribution
> that has to come from them is the one you ask for; everything before it, prepare
> as structured proposed action (name the step; note the resume point), but DO NOT
> execute or mutate anything. in v0, prepare instructions and orientation only — no
> draft prose. the why is theirs — one clause, rewritten to the size of the step, or
> the softer register if caring is what's making this heavy.

### 5.3 the tool, the schema, the guardrails

The turn writes with a tool, like every Pip outcome: `propose_unstuck(proposals=[…])`
— three items of

```
{kind, line ≤ 140,
 thing?: exactly_one_of({task_id}, {plan_id}, {label}),
 enough ≤ 100, minutes? ∈ {2,5,8,10},
 why? ≤ 120, why_register: matters|soft,
 action: start_task | pause_and_away | body_here | rest_here |
         start_company | point_to_sound,
 prepared_step?: {task_id, next_action},
 rationale_codes?: [allowed evidence ids]}
```

*(as built: `body_here` is the in-chair body action that fakes no away episode;
`rest_here` carries no `pause_current` — rest changes nothing, per §2.4.)*

Validated and bounded server-side: three distinct kinds or the call is refused with
the reason (the model may retry within the turn; the timeout ends it and the fallback
stands in); a `step` must name an owned, open task available
to this episode; every action must match its kind and may reference only objects in
the brief. No specific kind is required when the day has no suitable task. The server
assigns proposal UUIDs after validation; the model never invents identifiers.

`enough` remains a boundary the person can recognize, never a place to expose or
invent diagnostic evidence. When a rest proposal outside quiet hours needs support,
the model selects server-issued `rationale_codes` (for example `late_last_night` or
`past_quiet_hours`); the validator checks those codes against the snapshot and the UI
may use them to choose restrained copy. The tool stores proposals only. It performs
no workspace, focus, contact, file, app, or playlist mutation.

For v0, `prepared_step.next_action` is an action instruction, never draft content.
Generating an ugly first sentence for yellow work waits for the covenant's zone and
contribution-ledger enforcement to exist in code.

This is a tool-output-only turn. Incidental prose is discarded, not delivered and not
inserted into the daily room as an unexplained conversation. The action event may be
recorded for attribution, while the page shows the validated tool output only.

## 6. the fallback ladder

`src/services/stuck.py::fallback(snapshot: FocusDay, tasks, now) -> [3 proposals]`,
deterministic, model-free, always three kinds:

1. **step** — when a suitable task exists: a task with `needs_breakdown` and a
   `next_action` → its next action, 2 min; else the smallest-estimate untouched task
   → *open it and read what's there. two minutes. enough = you know what it's about
   again.* When no suitable task exists, **company** takes this slot: *sit here for
   five minutes; you do not have to choose anything yet.*
2. **body** — past three hours since the last run or the last finish → *water, then
   eight minutes outside*; else *stand up, stretch, come back*.
3. **rest** — past quiet hours or late-hours evidence → *sleep is the move*; else
   *close the list for now. nothing on it changes.* (§2.4).

Copy is authored (`lines.py` gets `stuck_fallback_*` pools). The fallback is also the
offline answer, plainly labelled. The app may use its last successfully cached today
snapshot to offer a task step; a cold offline launch has only sidecar focus/attention
state and therefore uses task-free company/body/rest proposals. It never pretends the
sidecar has canonical task data it does not have.

## 7. what it learns

**`StuckEpisode`** — `id (UUID), user_uuid, opened_at, surface, task_id?, status,
generation, proposals (json: each generation's three, server-assigned ids, in order),
accepted_kind?, execution_id?, run_ref? ({device_uuid, event_uuid}), outcome (json:
banked_seconds, stopped_at_boundary, kept_going, returned_after_away, rested_here),
closed_at`. **`StuckReaction`** is the append-only companion table:
`episode_id, proposal_id?, generation, reaction, at, request_id (unique)`; separate
rows make retries and concurrent surfaces safe without rewriting a JSON map.

Outcomes are filled from the device event stream (`session.started` / `session.ended`
with the episode and execution ids; `attention.returned` for an away episode) — no new
question is asked of the person. Device-local integer run ids are never treated as
globally unique.

When an episode has a good outcome (accepted + banked ≥ the container, or a rest
choice followed by a later return), a delayed evaluator may save one memory in the
person's words' shape: *when stuck around mid-afternoon, a body-first proposal then a
two-minute step on the willowden doc worked.* Next-day outcomes are evaluated next
day, not guessed by the same evening's settle turn. This adaptive memory write is
pushed out of v0; the raw episode remains available for dogfood analysis first. An
episode that ends in `closed` with nothing accepted is evidence too, silently.

**never:** "did that help?" as a modal, a rating, or a streak of unstuck days. If
Megan wants an optional reaction, it is one tap on the bubble after the fact, never a
prompt (the velvet antler rule: an unanswered ask is silently a no-answer).

## 8. guardrails and the covenant

- the page shows one proposal; it never shows the list, a score, a streak, a phase,
  or how many times the button was pressed.
- the rest branch is a full outcome. copy never converts rest into a plan.
- generating and browsing proposals changes nothing outside the episode. accepting a
  proposal may write one `next_action` or start/pause one local run, exactly as the
  visible action says. the rest branch changes nothing at all.
- covenant: v0 opens, restores, points at the passage, and writes action instructions;
  it does not draft prose for tracked work in any zone. yellow-grade preparation is a
  later capability, gated by a real zone field and contribution ledger rather than by
  prompt wording alone.
- coach pressure stays out of the deer's mouth (ROOMS_DESIGN §5): the brief carries
  minutes and kinds, never assessments.
- the button is exempt from the cadence ladder and the day cap: it's the person
  reaching out, not the house.

## 9. surfaces beyond the desk

- **telegram / rooms (later):** *i'm stuck* → the same episode, but still one
  proposal at a time. replies/buttons are `do this / different / too much`; the hidden
  alternatives never become a numbered list. accepting off-desk starts no clock but
  may write the accepted `next_action` and answers with the enough line. this is the
  away-from-desk version of the same promise.
- **morning brief:** `pick_first_thing` should skip tasks flagged `needs_breakdown`
  unless it offers to shrink them — the morning shouldn't lead with the stuck one.

## 10. willowden

This is the headline. The wiki page gets a section — *press it. that's the whole
ask* — with the page mockups, placed before Rooms. The Rust core makes `prepare` real:
open the file or app the step names, start the playlist, bring the page up as its own
window on a global hotkey, and (consented, per the privilege ladder) know that the
frontmost app has been a game since noon so a *transition agreement* (*after dinner,
put the prepared step in front of me*) can fire as a second `Calendar` rhythm with the
`stuck` brief — the person's own intention, at a time they chose, with *later* and
*i'm choosing leisure* as first-class answers.

## 11. build slices

Depends on dogfood slice 5 (`focus_day.py` — the brief is built from it). Then:

1. **server** *(BUILT 2026-09-09, branch `dogfood/stuck-server`: `src/services/stuck.py`
   evidence/brief/validator/fallback/store, `src/services/stuck_turns.py` the turn
   runner with the timeout, `src/services/tools/stuck_tools.py` `propose_unstuck`
   (terminal, pip's card allowlists it), director + briefer + helper + prompt hooks
   for `kind="stuck"`, migration `d8f2c4a7e1b9`, the three routes + TTL sweep in
   `src/web/server.py`, `Config.STUCK_*`; tests in `tests/test_stuck_*.py`.
   Sol's #89 round: every episode transition is a conditional update on
   (status, generation) — the row is the durable claim and a restarted server
   resumes persisted `thinking` rows from the poll, the open, and the sweep;
   acceptance re-checks the task is still open and not parked; rest carries no
   preparation; the pressed row's task id rides into the brief and the ladder;
   a second-generation ladder is three fresh kinds or an honestly `exhausted`
   card; "last night" is the single 23:00→06:00 window)* — `StuckEpisode` migration, the three routes, `kind="stuck"` stimulus +
   director branch, the brief in `enrich`, `propose_unstuck` tool + validation, the
   typed action schema, fallback ladder + its line pools, idempotency and 30s timeout.
   tests: three distinct kinds enforced; task-free fallback shape; generation checks;
   fallback becomes terminal; proposal creation and rest browsing mutate nothing.
2. **the page** *(BUILT 2026-09-11, branch `dogfood/stuck-page`: `view = "stuck"` in
   the main window's state enum, `components/StuckPage.tsx` over the pure
   `lib/stuck.ts` (copy, the cross-window request, which card, hide-the-why, the
   handoff), `api/client.ts` open/fetch/react wrappers. entry points: Home header
   right of the date line; companion den (full-width under the bubble) and bar
   (`stuck?` beside pause) — the companion writes `chordial.stuck.request` to
   localStorage and invokes the new Rust `show_main`, the main window reads it on
   the storage event or at mount (stale after a minute); tray `I'm stuck` →
   `show_main` + `app.emit("chordial:stuck")`. as built: "that's too much" is LOCAL
   until they leave — `too_much` lands when they close from the rest branch, so
   the door back costs nothing and the ledger stays true; `exhausted` shows the
   rest branch; the deer is "ask again" (a fresh request id, the old episode
   closed best-effort); a 409 on react re-fetches the episode and says "the card
   changed"; the fallback card carries "try the house again". the accept handoff is
   slice 2's minimal form — `startFocus` directly with `title: next_action` and the
   container, then `showCompanion` — slice 3 makes it the idempotent
   `run_mode="stuck"` execution with the boundary exit. sol's #91 round: the main
   window HIDES on close like the companion, so `show_main` always has a window
   to show and quitting stays the tray's or cmd-q's; the rail is dropped in stuck
   mode (no nav, no doors); a den press hides the den once the page is up while a
   running bar stays; no in-page cancel while thinking — closing the window is the
   cancel and the sweep closes what it left open.)* — route in the main window, thinking state (breathing dot, rotating
   lines), the card, rotation, the rest branch, the door back, hide-the-why. the
   button in den / bar / Home / tray.
3. **after do-this** *(BUILT 2026-09-11, branch `dogfood/stuck-after`: the sidecar's
   `start(..., execution={execution_id, episode_id})` is idempotent on the execution
   id — a retry while the run lives replays its state (`replayed: true`, no line,
   no second block), a retry after it ended is a 409; the run row carries
   `run_mode="stuck"` + both ids (sidecar column migrations), and so do its
   `session.started` / `session.ended` events (plus `target_minutes` on the end),
   while plain runs stay byte-identical; at target crossing the ticker announces
   `stuck_boundary` instead of `block_target`, never both; `pause(reason="boundary")`
   is "stop here" — banks under its own name with the `stuck_stopped` line; the
   window renders the two equal exits from `focus.run_mode` + over-target (the line
   may be hushed), "keep going" dismisses the pair for that run and the clock keeps
   counting, "stop here" then PATCHes the task's `next_action` to the v0 template
   resume point (*pick up where you stopped: <step>*) and the bubble says the
   evidence line; the server folds `session.started/ended` carrying the episode's
   own execution id into `StuckEpisode.outcome` (`banked_seconds`,
   `stopped_at_boundary`, `kept_going` from the target, `run_ref`) inside
   focus_flow's claimed transaction. sol's #92 round: the resume point is the
   SERVER's to write, from the durable `session.ended` (reason boundary) — only
   while the task still carries the accepted step untouched — so a dead network
   or a closed window can't lose it; the "keep going" choice persists with the
   run in the sidecar (`POST /v1/focus/boundary`, `focus.boundary_choice`) so a
   reload never asks again; the sidecar commits each run transition and its
   outbox event in ONE transaction (`store.transaction()`); the page keeps the
   accepted episode's execution and offers "start it ▸" on the same execution
   after a failed or lost handoff (the sidecar dedupes; "already ran" is final).
   NOT built: the 10 / 25 retarget chip after "keep going" — the sidecar has no
   retarget endpoint; a later nicety.)* —
   accept-time `next_action` write; idempotent execution handoff;
   step / switch / thread / company; `run_mode="stuck"`; the boundary exit with two
   equal buttons; `stuck_stopped` + resume-point write; globally attributable events.
4. **attention + body** — durable `AttentionState` / `AwayEpisode`; existing-signal
   rules (idle, freshness, focus state, explicit intent, surface visibility); body
   actions and the one re-offer on a real return. Drift remains block-scoped but reads
   the same attention transitions. tests: a paused run can still produce an away →
   return; stale samples produce neither; a mouse twitch is debounced; restart
   preserves an open away episode; ordinary idle with no block or explicit intent
   does not manufacture a stuck return.

Each slice is dogfoodable on its own; slice 2 with the fallback ladder alone is
already a working button. Slice 4 is part of the v0 promise because the body-first
bridge is one of the feature's most valuable different kinds, not optional polish.

### 11.1 deliberately pushed out of v0

- **adaptive memories and automatic "what worked" claims.** v0 records episodes and
  outcomes; a next-day evaluator and memory writes wait until the dogfood signals are
  trustworthy.
- **telegram and Rooms rendering.** they keep the one-card interaction when added;
  v0 proves the dedicated desk surface first.
- **opening files, apps, and playlists.** Willowden's privilege ladder owns these
  effects. Chordial v0 points; it does not operate the desktop beyond its own windows.
- **yellow-zone draft material.** first-sentence drafting waits for zone persistence
  and the contribution ledger. action instructions are enough for the first cut.
- **screen lock/wake and opt-in app-category signals.** the attention state is shaped
  to accept them, but v0 uses the signals already available and does not widen native
  observation permissions for this feature.
- **late replacement of a shown fallback.** v0 prefers a stable usable card. A future
  version can replace it only while untouched, with explicit generation semantics.
- **a true `hold today` rest action.** task parking may return later as an explicit,
  truthfully labelled choice if dogfooding asks for it. Automatic parking and
  automatic suppression of proactive contact are removed from the design, not merely
  postponed.
- **Willowden's global hotkey, separate window, transition agreements, and richer
  preparation.** the Chordial implementation proves the behavioral contract first.

## 12. dogfood measures (a couple of weeks, Megan plus a few ADHD users who won't configure anything)

- accepted proposal → banked at least the container (the step turned into an action,
  not just a timer)
- rotations before accept (3+ consistently = the brief is wrong, not the person)
- rest-branch rate, and the next meaningful app/session return after it (rest is not
  funnel failure)
- second episode the same day (the button is safe to press twice)
- the one question that decides it: *can it help on an afternoon with no energy to
  operate it?* — 2026-09-08 was the failing case.

## 13. open questions (Megan's calls)

| question | default in this doc |
|---|---|
| in-voice name for the page (the surface name stays plain) | none yet; "the clearing" is taken by the palette |
| main-window page vs its own window | main window in mvp; own window in willowden |
| does a body proposal pause a running clock automatically | only when its typed action means leaving the desk; in-chair actions do not |
| the container default | 2 min for a step; 5 for company; 8 for a walk |
| who speaks on the card | Pip's judgment, the deer's register; the voice pass decides |
| the why line default | on, hideable |
| an optional after-the-fact reaction | no, in v0 |

# The first run: cycle zero, the guided tour, and the driving hand

**Status:** product direction agreed; native-runtime architecture pending in
willowden, where the build lands. Sol review round applied 2026-08-24.

**Date:** 2026-08-24

**Scope:** a **brand-new user with zero data**. Returning and lapsed users
(including "back after a year, unfamiliar with the app") are deliberately a
*later* design — the tutorial-cycle mechanic looks like exactly the right
re-entry vehicle for them, seeded from their existing data instead of from
nothing, so this design keeps that seam open (§3) but does not specify it.

**Supersedes:** the *proposal* half (§2.6–§3) of
`docs/INITIAL_USER_STARTUP_FLOW.md`. That doc's *analysis* (§1–§2.5) still
stands and is the evidence base here: no product-level onboarding state, no
desktop account creation, a blank first Home, completion hanging on model
behavior, split-brain "onboarded" definitions. This doc replaces its
four-conversation-rooms answer with an app-first one.

**Relationship to the covenant:** read `docs/CREATIVE_COVENANT.md` and the
taper (§7 of ROOMS_DESIGN.md) before touching the reward mechanics in §7.

---

## 1. Principles

1. **App-first.** Every new user's first contact with chordial is the desktop
   app. Telegram/Discord are accessories — a pocket window the helpers can
   reach the user through when they're away from the desk — offered late,
   never required, never the front door.
2. **Action before interview.** Day zero means a first block *started*
   within about five minutes of signup and *banked* within about fifteen —
   not a life inventory. Intake happens opportunistically over the warm-up
   days; the first real cycle is proposed from **observed behavior plus the
   person's correction**. Telemetry is useful evidence, not truth — five
   novelty-period days are biased evidence at that — so cycle one is
   explicitly provisional.
3. **Always on the map.** The user always has a visible answer to "where am
   I in this?" — which is not the same as always being *inside* a cycle.
   Day zero begins inside cycle zero (the tutorial cycle); after that the
   strip shows whichever is true: the active cycle, an **upcoming** one
   ("next cycle begins monday" after a friday retro), or a resting state
   when nothing is scheduled. The cycle strip is permanent UI — it never
   nulls out.
4. **Chordial drives.** The product initiates: the tour opens itself, the
   morning pass has today's shape ready before the user asks, stalls trigger
   outreach. The user's job is to respond to one concrete invitation at a
   time, never to figure out what to click on a blank canvas.
5. **Less chat surface during ramp-up.** The deer + the cycle map are the
   primary surface. The room is a door the user walks through when ready.
   The pomodoro/deer pattern — visual state, ambient commentary — is the
   model, not "text chat with AI presence."
6. **Authored tour voice.** The tour's bubbles are hand-written deer lines
   (the sidecar `lines.py` philosophy): zero tokens, offline-safe, testable,
   never wanders. The model enters only at designated chat beats (meeting
   Vel, the cycle-one proposal).
7. **Deterministic outer journey, everything-else interiors.** Kept from the
   old doc. Code guarantees coverage and resumability; steps advance on
   *real user actions*, not "next" buttons, wherever possible.
8. **Kind gamification.** Additive-only recognition. No streak-shame, no
   overall grade, comebacks are celebrated. Details and guardrails in §7.

---

## 2. The front door

The desktop app must be able to **create** an account, not just link one.

- `POST /api/v1/signup` — invite/access code, display name, auto-detected
  timezone → creates user + first device credential in one step. First
  launch never bounces through a chat platform.
- Invite codes are the multi-user gate (and later map naturally onto the
  parents-buying-for-kids / classroom market — a family or cohort code).
  Single-operator deployments can run with a static code in env.
- **Second devices are approved from a first device.** An authenticated
  device mints a link code from its own Settings (a UI verb, not a chat
  favor); the LinkScreen-style code-entry remains only as the *receiving*
  end on the new device, and for the tether.
- **Identity contract to specify before the API freezes** (requirements,
  not yet designs): signup is idempotent and survives a lost response (the
  client holds a signup nonce; retrying returns the same account, never a
  duplicate); credential storage is the existing keychain path; account
  recovery after losing the only device needs an answer (see §10 — likely
  email-based, since there are no passwords by design); and the tour's
  offline story is stated honestly — authored lines are offline-safe, but
  *signup requires connectivity*, so first run is online-only and fails
  kindly ("can't reach the den — try again when you're connected"), never
  half-creates.
- Pronouns are not a signup field. Name + timezone are the only day-zero
  identity facts; pronouns and everything else arrive later through Vel,
  conversationally, where that question belongs — the answer space is
  open-ended ("none" is an answer, so is more than one set), which a
  dropdown can't honestly hold.
- The tether offer ("want me in your pocket?") appears at the earliest on
  day 2, framed as an accessory: *the helpers can reach you when you're away
  from the desk.* Never during the day-zero tour.

**Existing accounts:** out of scope here. The only requirement this design
places on them is structural: signup is a *separate* path from linking, and
the journey machinery must not assume it runs exactly once per user (see
§3's re-entry seam). The full returning/lapsed-user design comes later.

---

## 3. Cycle zero: the tutorial cycle

The onboarding journey and the first cycle are **the same facts seen two
ways**. The source of truth is a set of **persisted step facts** under a
`JourneyRun` record (see §8); the tutorial cycle is a *projection* of those
facts — never a second writable copy of progress — rendered in the exact
same cycle UI the user will live in forever. The tutorial teaches the cycle
system *by being one*.

Three things that look like "the journey" are deliberately separate runs
(`JourneyRun.scope`): **account onboarding** (cycle zero — once per
account), **device tour** (this machine's controls and OS permissions —
once per device; a second device learns its buttons without spawning
another cycle zero), and **re-entry / feature tours** (later designs).
Replaying the tour from Settings is a fresh throwaway run and never
mutates onboarding state.

- **Shape:** a short product-authored cycle (working default: **5
  user-local calendar days**, day 1 = the signup day — a late-night signup
  just makes day 1 a short bonus evening; tune from dogfooding), theme
  "settling in", tiny capacity, created at signup. Not proposed by Pip;
  owned by the product. Missed days don't extend or shame — growth is
  additive, a quiet day simply grows nothing. The nominal end date sets
  rhythm, but cycle zero actually *closes at graduation* (§ close below);
  overrunning it is allowed and quiet, and a stalled run is the driving
  hand's business (§6), never an expiry.
- **The re-entry seam (design open-ended, build later):** nothing about a
  tutorial cycle assumes an empty account. Its beats and commitments are
  authored, but *what they point at* can come from existing data when it
  exists — "tell me one thing" can become "here's what you left on the
  board; still true?", and the warm-up telemetry can blend with history. A
  returning-after-a-year user would get a re-tutorial cycle seeded this
  way. This design specifies only the zero-data instantiation; the code
  shape (journey re-runnable per user, beats parameterized by workspace
  state) should not preclude the seeded one.
- **Its commitments are the tour steps.** Roughly:
  1. *meet your deer* — the day-zero tour itself (§4)
  2. *bank your first block* — the day-zero payoff. "Bank," not "finish":
     stopping early still banks the minutes, and banking IS the lesson.
     Under the hood the block's states are modeled separately (`started`,
     `ended`, `banked`, `completed_as_planned`) so the commitment can be
     honest about which happened.
  3. *tell chordial what's on your mind* — seed 2–3 real tasks (quick-add
     or in-room, either counts)
  4. *meet vel* — first walk through the room door; Vel's welcome + one
     signature question (the existing intro ritual, shortened; the "make me
     yours" reshape stays optional and off the critical path)
  5. *a block a day while we settle in* — the warm-up rhythm, days 2–5
- Commitments tick in the panel like any other cycle. The first unfinished
  one gently pulses — there is always exactly one obvious next thing.
- Every step is **skippable** ("not this, not now" advances honestly and is
  recorded as deferred, not failed).
- **Close:** cycle zero closes with a mini-retro — a gentle Edwin cameo
  presenting a tiny, warm card (days you showed up, minutes banked; no
  grades) — and flows straight into **Pip proposing cycle one from the
  warm-up evidence, corrected by the person**: "you did five blocks,
  mostly mornings, ~20 minutes each — here's a small two-week experiment
  that fits what i saw. what did i get wrong?" The proposal is explicitly
  provisional (five novelty days can't reveal true capacity — it's a
  starting guess the first retro will correct), conservative, 3–5
  commitments, one next action each, shown as a reviewable artifact, then
  frozen. **Mechanically hardened:** the proposal is schema-validated and
  bounded (capacity caps, commitment count), with a deterministic
  fallback template so model availability can never block graduation.
  Freezing cycle one is the graduation moment (and a major award, §7).

**Intake without an interview.** The old doc's coverage contract (current
pressure, open loops, constraints, friction, support style, capacity) is
kept — as a tracked checklist on the journey record, filled
*opportunistically*: Vel asks one good question per day as a room moment
during warm-up, quick-added tasks fill the open-loops area, the session
ledger fills capacity. Areas can be `known | deferred | not_applicable`.
Nothing gates the start; by cycle-one proposal time, most of the map exists
without the user ever having sat through an intake.

**Never off the map.** When a cycle closes with a successor already
planned, the strip shows the **upcoming** cycle — theme, start date, a
quiet countdown ("begins monday") — so a friday retro flowing into a monday
start reads as a planned breath, not a gap. When nothing is scheduled, the
strip shows a **resting state** ("between cycles — the light stays on"),
with the one obvious action being the door to the planning room. A lapsed
user who returns after weeks is *in the resting state*, not staring at
null — and re-entry is a one-tap "start a fresh cycle" that Pip drafts
from whatever evidence exists.

---

## 4. The day-zero tour

Target: first block **started within ~5 minutes** of signup, **banked
within ~15**.

Mechanics: coach-marks pattern — the main window dims and spotlights one
region while the deer narrates via her speech bubble. **During the tour
the deer lives *inside* the main window** — she steps out into her
persistent always-on-top desktop window only at the block beat, when the
pomodoro machinery actually needs her (Megan's call). This keeps the
entire spotlight choreography single-window (no multi-window dimming to
coordinate, no platform-specific window juggling to test), and it makes
the step-out itself a designed moment: the app demonstrates what the deer
window *is* at the exact beat it becomes real. Steps advance on real
actions where one exists; pure-explanation steps advance on tap-anywhere.

Beats:

1. **Arrival.** The deer is right there in the window, and perks up.
   "hey. i'm your deer." One authored line of what chordial is: *we do
   small blocks of real things, together, on a map.*
2. **The map.** Spotlight the cycle strip: "this is your cycle — settling
   in, day 1 of 5. everything we do lives here. these five little marks are
   ours to fill." (Tutorial commitments visible, first one pulsing.)
3. **One real thing.** Spotlight quick-add: "tell me one thing you need to
   do. tiny counts. honestly, tiny is best." → advances when a task exists.
4. **The block — and the step-out.** "let's do a little of it. i'll sit
   with you — mind if i sit outside?" The deer steps out of the main
   window into her desktop window, and the block starts — the person
   picks **3, 5, or 10 minutes** (a picker, so the five-minute target is
   reachable and choosing is itself a small act of agency). Deer watches
   from her window; the authored ding lands; banking the block →
   **one hero celebration: the first object arrives on the shelf**, deer
   announcing it, confetti riding that single moment. The commitment tick
   and the first antler tine land *quietly* alongside — one celebration
   per moment, always. (If they stop early: the bank keeps the minutes,
   the deer says so warmly, the step still completes — banking IS the
   lesson.)
5. **The whole trick.** "that's it. that's chordial. small blocks, banked
   time, a map that fills in."
6. **The door.** Spotlight the room door: "the others live through here.
   vel keeps the front room — meet her whenever you like, no rush." Not
   required today; it's tutorial commitment 4, and the morning pass will
   extend the invitation tomorrow if untouched.

The tour is resumable at any beat (step facts survive restarts, skips, and
out-of-order arrivals — resume is computed from the facts, not from a
single pointer), replayable from Settings as a throwaway run that never
mutates onboarding state, and versioned — future feature tours are their
own `JourneyRun` scope, which is also how existing users meet new
features. OS-permission moments belong to the *device tour* scope, not to
account onboarding: a second device learns its controls and asks its own
permissions without spawning another cycle zero.

---

## 5. The always-visible cycle UI

- The cycle strip is **permanent** on Home (and a miniature — day dot +
  fill — on the deer window): cycle name/theme, day X of Y, capacity bar,
  commitments, the one pulsing next action. It renders four states —
  **tutorial, active, upcoming, resting** — and never returns null.
- The current `CyclePanel` becomes the strip's "real cycle" state; the
  one-tap "start a block ▸" affordance stays the primary verb of the app.
- The room door carries its existing unread nudge; the strip carries the
  *structural* nudge (what's next in the cycle). Two different questions,
  two different surfaces.

---

## 6. The driving hand

Chordial initiates — during onboarding and forever after. This strand is
mostly wiring on machinery that already exists (pulse, taper postures,
presence-aware routing from 7a, the tether).

- **Morning pass.** By the user's morning, today's shape is already drafted:
  the deer greets with one proposed first block (from cycle next-actions +
  recent rhythm), one tap to start. Opening the app is never a blank page —
  it's an invitation already extended.
- **Evening settle.** A soft close: what banked today, one line from the
  deer, tomorrow's seed. (Pairs with the existing lazy daily-room close.)
- **Journey-aware outreach.** The journey state feeds the pulse posture.
  Half-onboarded + stalled N hours → one gentle nudge, presence-routed
  (desktop active → deer bubble; away + tethered → telegram; untethered +
  absent → wait for next launch, which resumes the tour). The taper's
  "settling in" posture is literally this: maximum presence at the start of
  the arc, *because* it demonstrably fades on evidence — that's the consent
  story for being pushy early.
- **Stall ≠ failure.** Outreach copy always offers the smaller step
  ("shrink it?" / "not today — want me to hold it for tomorrow?"). The
  cadence ladder holds ignored chains (daily, then weekly, then the long
  floor — `CadenceGate` replaced the BackoffGate on 2026-08-27, and
  showing up by *doing* resets it like a reply); the deer never nags
  twice on the same beat.

**Background privileges are consentful, individually.** "Maximum presence
at the start" is a product posture — it is not itself consent. Each
privilege is asked for separately, **after the first payoff** (never
during the day-zero tour), with its benefit explained, and each is
reversible from Settings with visible pause/hush controls:

- launch at login;
- native notifications;
- idle / frontmost-application sensing;
- away-from-desk tethering (telegram);
- proactive morning/evening contact.

A "no" degrades gracefully (the feature explains what it loses, once, and
stays quiet), and the build must specify behavior through sleep, reboot,
app updates, offline stretches, notification denial, and a killed
background process — the driving hand may never depend on a privilege it
wasn't granted or a process that isn't running.

---

## 7. The dopamine engine

Two layers: a **permanent collection** and **kind seasonal ranks**. Both are
additive-only recognition of *process* — showing up, starting, banking,
returning. Never output volume, never a grade.

### 7a. Antlers — the seasonal rank

The cycle-rank metaphor the brand already owns: **antlers grow during a
cycle and shed when it ends, because that's what antlers do.**

- **The antler rack is a UI element in the cycle strip, not on the deer.**
  A sweeping set of horizontal antlers spanning the strip, filling
  **symmetrically outward from the middle** as the cycle is lived — each
  new tine lighting up left-and-right in mirrored pairs. It reads at a
  glance as "how much this cycle has grown," and it is beautiful when
  full. The deer sprite herself stays antler-free.
- Growth comes from process arithmetic: days you showed up, blocks
  started, returns after a gap. Pure deterministic functions in the
  taper/scorecard style — computed, never judged.
- **Additive-only:** antlers only grow within a cycle. Nothing shrinks
  them. A bad week just grows slower.
- **Shedding is natural, not loss.** At cycle close the rack sheds — one
  warm authored beat ("they'll be back — they always come back"), and the
  reached spread is recorded in the journal/den. Next cycle regrows
  fresh. Progression pressure lives *inside* the cycle; nothing infinite
  accumulates to grind or to lose. This is what keeps ranks compatible
  with the covenant and the taper: a user in earned quiet mode isn't
  bleeding a ladder position.
- Covenant guardrail, stated hard: Edwin's scorecard deliberately has **no
  overall grade**, and antlers must not sneak one in. Antler arithmetic
  reads presence/process events only — never execution-vs-plan, never
  scores.

### 7b. The collection — permanent

- Achievements are cozy objects that accumulate in the deer's corner of the
  world / a shelf in the clearing — a place to look at, not a number.
- **Trophies — the layer above achievements.** Completing a big *personal*
  goal or milestone (a real Goal/Plan finished, a thing that mattered)
  earns a permanent trophy: distinct from the ambient achievement objects,
  displayed with its story (what it was, when, what it took). This is the
  collection's deepest job — many ADHD users systematically dismiss,
  minimize, and forget their own past accomplishments, so the shelf is an
  external memory that argues back: *you did do this; here it is.* Wires
  naturally into the existing Wins/Goals machinery; revisiting the shelf
  should be a designed moment (retros can walk past it), not just a menu.
- Earned from the event stream that already exists (device events,
  observations, cycle events): first block, first finish, first task told,
  met each helper, first cycle frozen, first retro sat, ten blocks, first
  overtime ding, first block before 9am, first rainy-day return…
- **Comeback awards are first-class:** "you came back after four days away"
  is an achievement, said with complete sincerity. There are no streak
  mechanics anywhere — streaks punish exactly the person this product is
  for. We reward *returns*, not unbroken chains.
- Celebration moments: **exactly one hero celebration per moment** — when
  several things land at once (a block banks, a commitment ticks, a tine
  lights, an object is earned), the object arriving on the shelf is the
  hero and everything else updates quietly. The dopamine engine must never
  itself become noise; award announcements ride the same speech gates as
  everything else.

### 7c. Mechanics sketch

- `awards` table: earn-once awards are unique on `(user_uuid, award_id)`;
  event-processing idempotency is a separate guard on
  `(award_id, source_event_uuid)`. (A bare unique `source_event_uuid`
  would wrongly forbid one event from legitimately triggering two
  different awards.)
- An award service in the focus_flow style: scans applied events, pure
  rules, stamps in-transaction.
- Antler stage: derived per-cycle by pure arithmetic over the same ledger
  (no stored mutable "rank" — a projection, like everything else here).
- Award definitions authored in code with id + copy + predicate; versioned
  so new awards can backfill honestly or start-fresh explicitly.

---

## 8. Data model sketch

```text
JourneyRun
  user_uuid
  scope                  # account_onboarding | device_tour | reentry | feature_tour
  device_id?             # device_tour runs are per-device
  version                # journey schema version for this scope
  status                 # in_progress | complete | paused | abandoned
  started_at / completed_at

JourneyStepFact          # append-mostly; the actual truth
  run_id, step_id
  state                  # done | deferred | skipped
  occurred_at, source_event_uuid?

  # resume position, cycle-zero commitment states, and "what's my next
  # beat" are all PROJECTIONS over step facts — no current_beat pointer,
  # no second writable copy of progress. Out-of-order actions, skips,
  # replays, multiple devices, and newly inserted beats all reduce to
  # "which facts exist."

Award
  user_uuid, award_id, earned_at
  UNIQUE (user_uuid, award_id)              # earn-once
  idempotency: (award_id, source_event_uuid)

Signup
  POST /api/v1/signup {invite_code, name, timezone, client_nonce}
    -> user + device credential; idempotent on nonce (lost responses
       retry to the same account, never a duplicate)
```

All gates — chat routing's front door, pulse posture, UI next-step — read
the journey projection, replacing the `preferred_name` / `HelperState`
split-brain the old doc documented.

**Native runtime (willowden architecture decision, per review).** There is
no sidecar *process* at all: the long-running Tauri app is itself the
native engine. React windows own rendering, coach marks, and input; the
Tauri/Rust core owns the local SQLite, the focus clock, the journey
projection, authored-line selection, collectors, local scheduling and
notification gates, and the sync outbox; the cloud owns accounts, the
shared workspace, rooms/models, and cross-device reconciliation. This
eliminates the loopback server, port ownership, process supervision,
token handoff, and multi-process startup delay of the current
python-sidecar shape. The UI sends *commands* ("start block") and renders
state; it never decides that a domain step completed — the Rust core
records the event, advances the projection, and syncs it.

---

## 9. Sequencing (for willowden)

Implementation lands in the willowden repo after the mirror + rename
slice. Sliced **vertically around an end-to-end first run** — the first
meaningful milestone is the complete five-minute experience, not any layer
in isolation:

1. **Native foundation** — the in-process Rust core (§8): local database,
   focus clock, outbox, and account registration against the server.
2. **The day-zero vertical slice** — signup → task → block → durable
   resume → the one hero celebration. Includes the strip's tutorial state,
   the coach-marks tour, and the deer step-out. *This is the milestone.*
3. **Warm-up** — days 2–5: Vel's room moments, morning pass / evening
   settle, the consent asks (§6), scheduling and catch-up for absent
   users.
4. **Graduation** — cycle-zero mini-retro + the validated, bounded,
   fallback-guarded cycle-one proposal (mostly existing cycle-rooms
   machinery, journey-aware).
5. **Collection + antlers** — awards service, shelf, trophies, rack and
   shed/regrow beats — *after* the event vocabulary is stable. (Slice 2
   ships the day-zero shelf moment with a minimal hardcoded award so the
   hero celebration exists from day one; the general engine comes here.)

**Document plan (per review):** when this moves to willowden, split into a
durable product spec, a native-runtime architecture decision, the
migration audit (already extracted — see §11), and per-slice
implementation plans with acceptance criteria. A deliberate brand-language
pass happens there too — no mechanical chordial→willowden rename.

---

## 10. Open questions — to be settled by dogfooding

- Tutorial cycle length: 5 user-local calendar days is the working
  default — 3 if dogfooding says the warm-up drags.
- Naming: the collection place ("the shelf"? "the clearing"?), antler-tine
  milestones, trophy presentation.
- Invite-code shape for v1 (static env code vs minted codes).
- **Account recovery** after losing the only device: likely email-based
  (there are no passwords by design; a recovery code shown once at signup
  is exactly the artifact this audience loses). Must be answered before
  the signup API freezes.

Settled in review (2026-08-24):

- **Name + timezone are UI signup fields**; the deer uses the name
  immediately, which lands warmer than asking twice.
- **Pronouns are asked conversationally by Vel, never a signup field** —
  the answer space is genuinely open ("none," multiple sets, "skip this,"
  anything) and a form flattens exactly the people the question matters
  most for.
- **Antlers render as the cycle strip's rack** (symmetric center-out
  fill), not on the deer sprite.

Settled in Sol's round (2026-08-24):

- **The deer is embedded in the main window during the tour** and steps
  out to her desktop window at the block beat (Megan's placement — the
  step-out happens when the pomodoro machinery first needs her, and
  teaches what the deer window is).
- **Day-zero block length is the person's choice: 3, 5, or 10 minutes.**
- **Timing contract:** block *started* ≤ ~5 min from signup, *banked*
  ≤ ~15.
- **Cycle zero = 5 user-local calendar days from signup day**, closing at
  graduation, never by expiry.
- **One hero celebration per moment** — the shelf object is the hero;
  ticks and tines land quietly.
- **No sidecar process in willowden** — the Tauri/Rust core is the native
  engine (§8).

---

## 11. The app-first audit

The full repo sweep for places chordial-mvp conflicts with principle 1
lives in **`docs/APP_FIRST_AUDIT.md`** (extracted from this doc per
review — it's a migration inventory that ages with the code, and it
shouldn't age the product spec with it). Its headline: the delivery
plumbing is genuinely app-ready; the blockers are all in identity,
onboarding state, and capability surface, not transport. The audit ends
with a mapping of its eight findings onto §9's build slices.

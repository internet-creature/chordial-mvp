# App-first audit of chordial-mvp

**Date:** 2026-08-24, branch `mvp/first-launch-fixes`

**What this is:** a repo sweep for places the current code conflicts with
the app-first principle of `docs/FIRST_RUN_DESIGN.md` §1.1 — the desktop
app as the primary front door, chat platforms as accessories. Extracted
from that doc so the product spec can stay durable while this inventory
ages with the code. Line numbers are pointers, not pins.

**The headline: the delivery plumbing is genuinely app-ready — the
blockers are all in identity, onboarding state, and capability surface,
not transport.**

## The eight biggest conflicts

1. **No app signup, and the API is structurally incapable of one.**
   `UserManager.get_or_create_user` (`src/managers/user_manager.py:14`) is
   the only production `User()` writer; its only caller is
   `ChatService.process_message` (`src/services/chat_service.py:135`) — an
   account exists only as a side effect of a chat message arriving. Every
   `/api/v1` route is device-token-gated (`src/web/server.py:548`); the
   sole unauthenticated route only *redeems* link codes. Web login's own
   docstring: "the user asks chordial **in chat** for a login code"
   (`src/web/auth.py`).
2. **The desktop cannot mint its own link code.** The `link_device` model
   tool (`src/services/tools/link_tools.py:100`) is the sole production
   path; the only alternative is `scripts/dev_db.py link-code` (dev-only,
   hardcoded uuid). The packaged-app README literally tells users to DM
   the telegram bot (`app/README.md:108`).
3. **Nothing ever initiates an introduction.** `introduction` is only a
   classification of an *inbound* message (`chat_service.py:166`); no code
   constructs one proactively. Worse, the pulse's `OnboardingGate`
   (`src/services/pulse_wiring.py:198`) and `get_scheduled_users`
   (`user_manager.py:162`) both actively *suppress* proactive contact for
   un-onboarded users — chordial cannot drive; it can only answer.
4. **`preferred_name` is the onboarding key and only a chat tool writes
   it.** `set_preference` (`src/services/tools/preference_tools.py:26`) is
   the sole writer of name/pronouns/timezone; no endpoint sets them. Two
   different "onboarded?" definitions coexist (`needs_onboarding` vs
   `_still_introducing`) — the split-brain the journey projection
   replaces.
5. **Every write-side capability except quick-add-task is model-tool-only.**
   Commitments/freeze/scope-change (`cycle_tools.py:387`),
   plans/goals/cycles (`workspace_tools.py:966`), `meet_guide`
   (`intro_tools.py:170`), preferences, memories — none have `/api/v1`
   equivalents. The app API today: add a task, set task status, and read
   everything else. A tutorial cycle or guided tour cannot be driven by
   app UI without a new capability surface — in willowden's shape, the
   journey service owns these verbs; the UI asks "what's my next beat,"
   it never orchestrates cycle writes itself.
6. **The fresh-user screens are blank and their copy points at chat.**
   `LinkScreen` demands a code its own comment says comes "from vel in
   chat" (`app/src/components/LinkScreen.tsx:8`); `CyclePanel` returns
   `null` (`CyclePanel.tsx:90`); Home greets namelessly with "nothing on
   the list" (`Home.tsx:177-193`); the empty room says "say hi — someone
   will hear you" (`Room.tsx:277`); link-failure copy says "ask chordial
   for a fresh one" (`server.py:404`). The council API even masks a
   `not_met` chair as `active` (`server.py:770`) — the one signal the app
   could have used to detect "brand new user" is papered over.
7. **The system cannot run app-only out of the box.** `ENABLE_DISCORD`
   defaults `true` (`config.py:290`) with no token guard, and a missing
   token crashes the whole process at runtime (`main.py:26,161`;
   `discord_bot.py:78`). Every documented dev loop already sets
   `ENABLE_TELEGRAM=false ENABLE_DISCORD=false` by hand — the defaults are
   wrong for the app-first world.
8. **What's already right:** `'app'` is a first-class presence-routed
   proactive target (`pulse_wiring.py:244`), the router registers it before
   chat platforms (`main.py:370`), and the front-door check runs on every
   inbound surface (`chat_service.py:167`). Secondary gaps: an `absent`
   app-only user gets zero proactive contact with no catch-up queue
   (`pulse_wiring.py:259`); `AppInterface` has no `send_choice`, so the
   app can't render yes/no prompts (`base.py:40`); the rewind tether
   vanishes entirely without a chat platform (`main.py:421`,
   `rewind_tether.py:65`).

## How this maps onto the build slices (FIRST_RUN_DESIGN §9)

- Slices 1–2 (native foundation + day-zero vertical) resolve items 1–4
  and 6: signup, the journey projection as the single "onboarded?" truth,
  retiring the OnboardingGate's suppression in favor of journey-aware
  outreach, and replacing the blank chat-pointing screens.
- Item 5's capability surface is the journey service, built across
  slices 2–4.
- Item 7 is a small config PR that can land in chordial-mvp *now*
  (discord token guard + default flip) — it's a bug by any standard.
- Item 8's gaps (catch-up for absent users, `send_choice` for the app)
  belong to slice 3.

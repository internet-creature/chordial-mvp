# MVP frontend review — September 10, 2026

Implemented on `codex/forest-frontend`, from `main` at `45d3dbd`, in a separate
worktree. The ongoing `dogfood/stuck-server` branch and its server feature are
not part of this change. No production API calls or database access were needed.

## Reported task mismatch

**Confirmed frontend defect:** `DeerWindow` loaded `/api/v1/today` on mount,
credential/local-sidecar connection changes, and its own actions. It had no
subscription to room deliveries, no task polling, and no foreground refresh.
`Home` also loaded its task list only on mount. A successful conversation edit
could therefore leave both lists stale indefinitely. Failed task reads were
silently discarded, so the companion's green local-sidecar light could look
healthy while its server task data was stale.

The fix extracts a shared `useToday` hook. Completed chat POSTs, durable council
socket deliveries, socket reconnection, and successful task mutations now
invalidate the task views. A same-window event and a cross-window storage
revision carry this signal; neither carries task content or credentials.
Foreground/focus, return online, and a 30-second visible-window poll cover
missed events, another device's edits, reopening, and day rollover. There is
still only one server room socket, owned by the main app.

Reads are serialized with a trailing refresh when an edit arrives during an
in-flight read. Old session responses are ignored, unmount aborts the request,
and each read has a 15-second timeout and bypasses the HTTP cache. The last good
list remains visible through an outage, accompanied by a separate task warning
and a retry control. Initial loading is distinct from an empty task list.

**What remains unproven:** the screenshot does not establish whether that
specific conversation actually persisted its edits. The current server's today
endpoint uses `WorkspaceStore.list_tasks()` with open statuses only;
`deprioritized` is a closed status. A successfully saved deprioritization should
disappear on a fresh read. “Set aside for today” is a separate server state and
remains visible in its own group. The UI deliberately does not infer mutations
from the assistant's wording. Verifying the original turn needs its tool/audit
records or a read-only check of the affected tasks on the VPS, including the
server revision deployed at that time. No server changes are included here.

## Companion recovery

The existing close interception already hid the companion without destroying
it, and a tray toggle could restore it. The problem was discoverability: there
was no entry in the main UI and the tray action combined show and hide.

An always-accessible **open companion** button now lives at the bottom of the
sidebar. It invokes a native command that unminimizes, shows, and focuses the
existing window. The tray now has explicit **Show companion** and **Hide
companion** actions. The close controls explain where to reopen it. None of
these actions stops, restarts, or resets the sidecar clock. In a browser preview,
the button opens/reuses a named companion popup.

## Visual pass

- Pine backgrounds and restrained sage surfaces, with warm off-white text.
- Pastel blush reserved for accents, focus outlines, and the user's chat bubble.
- System sans-serif for reading; local monospace labels and clocks for a subtle
  terminal feel. No font downloads or added production UI dependencies.
- A clearer home hierarchy, a prominent room entrance with its unread count,
  visible connection text, and persistent companion access.
- Flat controls and quieter borders across rooms, archives, cycles, linking,
  scoping, set-aside actions, rewind controls, and the timer.
- A 320 × 560 companion with two-line task titles; selected rows reveal the full
  title. The compact timer remains 440 × 56. Both use the same theme tokens.
- Keyboard focus, labelled quick-add/refresh controls, reduced-motion support,
  and a scrollable council area that leaves the companion button available at
  the main window's 760 × 520 minimum size.
- The supplied artwork replaces the default Tauri icons. Its centered square
  source is retained at `app/src-tauri/icons/source.png`; the UI uses a small
  256px copy. Native window chrome is set to dark to match the content.

## Validation and limits

Passed: production frontend build, 93 frontend unit tests, 8 browser tests,
and 18 native Rust tests.

`npm run build` checks TypeScript and builds both window entries. `npm test`
covers existing frontend behavior plus refresh races, session disposal, failure
recovery, timeouts, and cancellation. `npm run test:browser` uses two independent
pages and completely intercepted server/sidecar traffic to exercise:

- Conversation edits reaching an already-open companion.
- Server socket deliveries and companion edits reaching an already-open home.
- Failed task reads retaining the last list and recovering on retry.
- Focus refresh and 30-second recovery without a main window.
- Opening and reopening the companion from the sidebar in browser preview.
- Home, conversation, archive, link, task-detail, minimum-size, and timer layouts.

The browser suite writes screenshots into `app/artifacts/` (ignored by Git).
Run `npx playwright install chromium` once before running it on a new machine.
`cargo test --manifest-path app/src-tauri/Cargo.toml --lib` compiles the native
command and exercises window-placement and sidecar-lifecycle logic. The native
show/hide interaction itself still needs an in-app smoke test on the next build;
the user's running app was not replaced or restarted. Production persistence,
macOS keychain behavior, and Windows window chrome were not exercised by the
mocked browser tests.

## Follow-up opportunities

`DeerWindow` still owns most clock, rewind, and task-detail interaction state in
one large component. Extracting those independently would make future features
easier to review; this pass extracts only task synchronization. A structured
server mutation receipt/event would ultimately give the UI a stronger signal
than refreshing after every completed council turn. A backlog browsing surface
would also make it easier to find tasks after deprioritizing them; today's API
intentionally excludes them.

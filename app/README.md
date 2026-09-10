# chordial desktop app 🧸

The Tauri shell for chordial — rooms, cycles, and the council.
Design: [`docs/ROOMS_DESIGN.md`](../docs/ROOMS_DESIGN.md) (authoritative).

- **frontend:** React + TypeScript (Vite), in [`src/`](src/)
- **shell:** Tauri 2 (Rust), in [`src-tauri/`](src-tauri/) — will grow the
  collectors (frontmost bundle id, idle) and the deer/tray windows
- **sidecar:** the Python ambient engine — bundled into release builds as
  a frozen binary (phase 7b, [`docs/PACKAGING.md`](../docs/PACKAGING.md));
  during development it still runs side by side from the repo root, and
  debug builds never spawn it

## dev

Prerequisites: **Node `^20.19` or `>=22.12`** (required by the locked Vite
version), plus the Tauri prerequisites for your OS
(<https://tauri.app/start/prerequisites/>):

- **macOS:** Xcode CLT and Rust via
  `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- **Windows:** Visual Studio Build Tools (the "Desktop development with
  C++" workload), the WebView2 runtime (preinstalled on Win 11), and Rust
  via [rustup](https://rustup.rs) (default MSVC toolchain). The activity
  collector is macOS-only for now (`cfg`-gated) — the app builds and runs,
  the deer just has no drift/hush senses yet.

The sidecar also needs **Python ≥3.10 + Poetry** (`poetry install` at the
repo root).

The shell is a client: it needs the chordial server running beside it, and
the deer window needs the sidecar (the on-device engine that owns the
focus-block clock). The full dev loop, from the repo root:

```bash
# terminal 1 - the server (a disposable sqlite state; see scripts/dev_db.py)
poetry run python scripts/dev_db.py fresh     # or: seed
DATABASE_URL=sqlite:///chordial_dev.db ENABLE_TELEGRAM=false \
  ENABLE_DISCORD=false poetry run python main.py

# terminal 2 - a one-time device link code (the no-telegram path)
poetry run python scripts/dev_db.py link-code

# terminal 3 - the sidecar (loopback engine on :8485; own sqlite file)
poetry run python -m src.sidecar

# terminal 4 - the shell (opens the main window AND the deer window)
cd app && npm install && npm run tauri dev
```

If `tauri dev` complains about a missing external binary, run
`bash packaging/build_sidecar.sh` once from the repo root — the CLI wants
the frozen sidecar file to exist even though debug builds never spawn it.

Paste the code into the app's link screen and you're in. On a `fresh` db the
first message runs the real front-door introduction (vel greets you); `seed`
skips introductions with a lived-in workspace.

The deer window hands the sidecar your device credential automatically
(same-origin localStorage), so completed focus blocks flow up to the server
through the sync contract even after restarts — the sidecar's outbox
retries until acked.

## the collectors & gates

In the Tauri shell, a Rust thread polls two signals every 2 seconds —
the frontmost app's **bundle id** and **idle seconds** — and posts them to
the sidecar. That thread ([`src-tauri/src/collector.rs`](src-tauri/src/collector.rs))
is the privacy boundary: window titles, urls, and content are structurally
absent, and bundle ids never leave the machine (the server only ever sees
derived events like `drift.detected`). The tray icon can tuck the deer
away and call her back.

The sidecar turns those signals into at most one gentle drift check-in per
episode (and a welcome back), gated by arithmetic, tunable by env:

```bash
SIDECAR_DRIFT_IDLE_MINUTES=10   # idle this long mid-block = one soft check-in
SIDECAR_LINES_PER_HOUR=6        # unprompted-speech budget, rolling hour
SIDECAR_QUIET_HOURS=            # e.g. "22-8" (machine-local; empty = off)
SIDECAR_BLOCKLIST=              # meeting apps that hush the deer entirely
                                # (defaults include zoom/teams/facetime/webex)
```

`npm run dev` alone runs the Vite frontend in a browser without the Tauri
shell — useful for pure UI work. `npm test` runs the frontend unit tests
(vitest); `npm run build` typechecks and bundles.

The frontend talks to `http://localhost:8484` by default; point it elsewhere
with `VITE_CHORDIAL_SERVER` in `app/.env.local` (a *destination*, so it lives
in the app CSP's `connect-src` — not in `APP_ALLOWED_ORIGINS`, which lists
the *webview's* origins the server accepts requests from).

## running against the real server

To run the shell as a client of a remote chordial server (no local server
needed — only the sidecar runs on-device):

```bash
# app/.env.local
VITE_CHORDIAL_SERVER=https://api.internetcreature.dev
```

The origin must be listed in the app CSP's `connect-src`
([`src-tauri/tauri.conf.json`](src-tauri/tauri.conf.json) — https *and* wss)
and the server must be reachable over TLS (it binds loopback; a cloudflared
tunnel fronts it). The deer window hands the sidecar this origin during the
handshake, so the sidecar's outbox pumps to the same server. Linking uses a
device code minted by the council: DM the telegram bot and ask to link this
device (the `link_device` tool) — codes expire after ~10 minutes, so paste
promptly.

## security posture

- Production CSP is restrictive (`default-src 'self'`). `connect-src`
  permits only IPC, the sidecar, and the chordial server (the local dev
  server plus the real server origin, `api.internetcreature.dev`).
  The server allows the app's origins (`tauri://localhost` on macOS/linux,
  `http://tauri.localhost` on windows, the vite dev origin) via a scoped
  CORS allowlist on `/api/v1` only — configured with `APP_ALLOWED_ORIGINS`
  on the server side. The sidecar keeps the same list in
  `SIDECAR_ALLOWED_ORIGINS`.
- The device bearer token: packaged builds keep it in the OS keychain
  (macOS Keychain / Windows Credential Manager, via the shell's
  `credential_*` commands — the stronghold plugin was passed over: it wants
  a password-derived snapshot, and the OS keychain is the thing actually
  meant). Dev builds keep webview localStorage — an unsigned debug binary
  changes identity every rebuild, and macOS would prompt each time. First
  packaged run migrates a localStorage-era token in and removes the
  plaintext copy.
- The template's opener plugin was removed (nothing opens external URLs yet).
  If/when the app needs to open links, re-add it with a scoped URL allowlist,
  not `opener:default`.

## forest frontend review

The forest theme and companion synchronization/recovery work are documented in
[`docs/FRONTEND_REVIEW.md`](../docs/FRONTEND_REVIEW.md). The main sidebar's
**open companion** button and the tray's **Show companion** action restore a
hidden companion without restarting its clock.

Browser regression tests use mocked server and sidecar responses, so they do not
need the VPS or a local database:

```bash
npm ci
npx playwright install chromium
npm test
npm run test:browser
```

The source artwork for desktop icons is `src-tauri/icons/source.png`. To
regenerate Tauri icons, run `npm run tauri -- icon src-tauri/icons/source.png`.
The frontend uses `public/chordial-icon.png`, a smaller copy of that artwork.

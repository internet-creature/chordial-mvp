# The workspace

*Written for someone who has never seen this codebase. Deeper detail:
`NATIVE_WORKSPACE_DESIGN.md` (schema authority), `NATIVE_MIGRATION_PLAN.md`
(how we got here).*

The workspace is chordial's memory of what the user is *doing* — their plans,
tasks, goals, wins, notes, and upcoming occasions. It lives in the app's own
database (Postgres in prod, sqlite in dev) as eight tables, and the helpers
read and write it through ordinary tool calls in conversation. There is no
external system of record: if it's not in these tables, chordial doesn't know
about it.

Historical note: v2 used the user's Notion workspace for this role. That
integration was retired in phase D of the native migration; a thin Notion API
client (`src/services/notion/client.py`) survives for a planned optional
extension — a per-user, one-way *mirror* that pushes workspace items into a
user's own Notion for browsing. Postgres is the source of truth either way.

## The tables

All in `src/database/models.py` under the "native workspace" banner; every
mutation goes through `WorkspaceStore` (`src/services/workspace/store.py`),
which owns the invariants.

| table | what it holds |
|---|---|
| `plans` | larger efforts ("write the novel", "learn piano"). Carry a helper attribution and `last_activity_at`, stamped as a side effect of activity on their tasks/notes. |
| `tasks` | day-to-day to-dos. Optional `scheduled` (a user-local calendar date), optional links to one plan and one cycle. |
| `cycles` | sprint-like time boxes with a focus line. |
| `goals` | direction-setting intentions, looser than plans. |
| `wins` | things that went well, logged as they happen; surfaced weekly in the agenda. |
| `checkins` | morning/evening (and ad-hoc) check-in records: energy, notes, which plans got attention. |
| `notes` | freeform jots — ideas, fragments, "story idea for the book". Never surfaced in the agenda; brought up when work touches their plan. |
| `occasions` | dates that matter (birthdays, appointments), with optional recurrence that rolls forward automatically. |

Two conventions to know:

- **Lifecycle (design §2.0):** rows are never deleted by conversation. Closing
  states ("Done", "Shipped", "Not relevant anymore") stamp `closed_at`; list
  queries filter to open-by-default, so history persists but reads as
  historical.
- **Public ids:** tools address rows as `t42` / `p7`-style ids, which
  round-trip safely through model conversation. Vocabularies (statuses,
  priorities, energies) live in `src/services/workspace/vocab.py` and are
  canonicalized case-insensitively, including the legacy display forms.

## The tool surface

`src/services/tools/workspace_tools.py`, registered unconditionally in
`build_default_registry()`:

- **Core** — `list/create/update_task`, `archive_tasks` (a named set → 
  deprioritized in one transaction, all or nothing), `list/create/update_project` (aliases
  that operate on plans; they predate the plan rename and retire with the v4
  persona-prompt pass), `list/create/update_plan`, `list/create/update_cycle`.
- **Extras** — `create/update/list_goals`, `log_win`/`list_wins`,
  `log_checkin`/`list_checkins`, `jot`/`list_notes`/`update_note`,
  `log_occasion`/`list_occasions`/`update_occasion`.

Personas see subsets via their card allowlists (an unknown name in an
allowlist crashes at startup, on purpose). Helper attribution on
wins/check-ins/notes/occasions comes from the acting-helper contextvar, never
from the model.

**A blank argument is an omitted argument.** `src/services/tools/inputs.py`
wraps every workspace and cycle-spine handler so a `""`/whitespace value
never reaches it, and the store's resolver matches nothing on a blank ref
(it used to fall to the substring tier and match *every* row). Clearing a
field is therefore always explicit — `update_task(clear_next_action=true)`
— never an empty string. This is the product-side half of the padded-call
fix; the wire-side half is the dainframe's OpenAI provider rendering tools
in strict mode (optionals nullable, nulls stripped), so a model that pads
optional fields it isn't setting says `null` instead of `""`/`0`/the first
enum value. Malformed dates come back as a promptable correction, and
`list_tasks(title_prefix=…/title_contains=…)` exists so a bulk change ("every
task starting with Pomodoro") can list its whole target set before touching
it; every filter (priority included) is applied in SQL before the page, and
the first line reports the set's size ("showing 25 of 31 …") so a sweep never
quietly becomes "the first page". `archive_tasks(tasks=[ids])` then lets the
set go in one transaction. The heavier version of this - server-held
selections, receipts, replay, undo - is parked for Willowden in
`CONVERSATIONAL_TASK_BATCHES_DESIGN.md`.

## The agenda

`src/services/workspace/agenda.py` builds two things live from the store (no
cache — local queries are microseconds):

- **`get_digest`** — a compact (~150–400 token) "today picture": tasks today /
  overdue / in progress, current cycle + focus, plans by helper, wins this
  week, occasions within 3 days. The orchestrator injects it as ambient
  context on the *volatile* current turn, so it never touches the prompt
  cache. The system prompt carries one standing line telling the model to
  treat it as ambient awareness, not a checklist.
- **`get_payload`** — the structured buckets the completion reconciler reads
  to notice "oh, they just said they finished t42" and mark it Done.

"Today" is always the *user's* local calendar date; `scheduled` and occasion
dates are plain user-local dates.

## Config

- `AGENDA_ENABLED` (default true) — the ambient digest on/off switch.
- `WORKSPACE_MAX_PAGE_SIZE` (default 25) — row cap per list call.
- `RECONCILER_ENABLED` (default true) — the passing-mention completion pass.

## Testing

`tests/test_workspace_store.py`, `test_workspace_tools.py`,
`test_agenda_native.py`, plus the reconciler e2e in
`test_completion_reconciler.py`. Both lanes: plain `poetry run pytest`
(sqlite), and `TEST_DATABASE_URL=postgresql+psycopg://…/chordial_test poetry
run pytest` (postgres; the db name must contain "test"). See
`DEV_DATABASE.md` for the dev-database workflow.

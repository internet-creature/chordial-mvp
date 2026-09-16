> **Status (2026-09-16): parked for Willowden.** The incident that prompted
> this turned out to be a half-deployed fix (chordial pulled, the dainframe
> not - `OPERATIONS.md` "the seam"), and the padded-call fix works as intended
> once both are live. Of this design, the mvp took the small, real pieces
> (`fix/task-sweeps`): priority filtered in SQL before the page, a
> matched-total line, `title_prefix`, and `archive_tasks(ids)` in one
> transaction. Server-held selections, receipts, replay, typed outcomes and
> undo are the right shape for a multi-user, multi-process Willowden and are
> kept here as that design's seed - not as the mvp plan.

# Reliable conversational task batches

Design proposal, 2026-09-16. Implementation has not started.

The model should translate the user's request into a task selection and an
operation. Chordial should own selection completeness, scope semantics,
transactional execution, retries, and reporting the actual outcome. The first
operation is archive; the same selection service can later support completing,
rescheduling, or setting aside a group without building a general action engine.

## Evidence and corrections to the supplied diagnosis

Reviewed Chordial at `63db9d6` and the adjacent dainframe at `87d9707`. Production
observations below come from the supplied conversation, not a fresh server read.

| Claim | Assessment |
|---|---|
| Blank argument handling fixed the old resolver error. | Consistent with `inputs.py` and the reported trace. It cannot remove a legitimate-looking but unintended enum, number, or boolean. |
| Priority filtering excluded all eight NULL-priority tasks. | Correct if the production rows are as reported. Reproduced locally with synthetic rows. `priority=high`, `medium`, or `low` excludes NULL. |
| The model must supply a real priority. | Conditional on the effective wire schema. An optional property can be omitted in ordinary JSON Schema. Responses can normalize schemas when strict is unspecified; inspect effective schemas rather than inferring behavior from source definitions alone. |
| The provider fix does not exist. | False for this developer checkout. Commits `d03530b` and `87d9707` implement strict rendering and null pruning; the running local Python imports that checkout. The server may have been stale at the time of the trace. |
| Strict mode does not fix enums. | The local implementation makes optional enums nullable in both `type` and `enum`, then removes introduced nulls. This addresses padding, but cannot guarantee the model chooses the intended filter. |
| Dropping the date was correct. | Not established. The UI includes carried-over work; “today's list” and “scheduled today” mean different sets. Removing an explicit date can expand a mutation beyond authorization. |
| Five iterations make success impossible. | Too strong. The loop executes multiple calls from one response with `asyncio.gather`; one list round and one round containing seven updates can fit. Sequential one-task rounds exceed the budget, and parallel execution does not provide an atomic batch. |
| `is_error=False` proves success. | It means the handler returned normally. Empty search is a valid result, while current validation/ambiguity failures also return ordinary strings. Neither establishes that a mutation occurred. |

Additional defects found:

- `_list_tasks` filters priority **after** `WorkspaceStore.list_tasks` applies the
  limit. A matching high-priority task beyond the first page appears absent.
- The default cap is 25 and the response has no total, cursor, or truncation
  indicator. “Archive all” can silently become “archive the first 25.”
- The only title operation is substring matching. Its description suggests it
  for prefixes, but `Pomodoro` also matches `Review Pomodoro notes`; it does not
  implement “starts with Pomodoro #”.
- Archive is an implicit translation to `deprioritized`. A generic update also
  exposes unrelated fields that a padding model could accidentally change.
- The cap path removes tools and requests prose without supplying a typed
  reason or mutation receipt. The model can invent a capability failure.
- Today's buckets are implemented separately in the agenda and HTTP payload.
  Conversational selection needs the same semantics, not a third approximation.

Relevant sources: `src/services/tools/workspace_tools.py`,
`src/services/workspace/store.py`, `src/services/workspace/agenda.py`,
`src/web/server.py`, `config.py`, and dainframe's `providers/openai.py`,
`tools/registry.py`, and `loop/agent_loop.py`.

OpenAI's [strict-mode documentation](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)
requires closed objects and required properties, with nullable types representing
optional values. Responses may normalize schemas when strict is omitted.
Explicit schema construction avoids depending on those defaults.

## 1. Deploy and verify the existing provider fix

First verify the server process's imported module path, both repository SHAs,
provider configuration, and effective tool schema. Chordial has an editable
dependency on the adjacent dainframe checkout; updating only Chordial does not
update the provider. Follow `OPERATIONS.md`: update both repositories, install,
restart, and verify the startup stamp.

Keep the local strict conversion and product-side blank tolerance. For the
actual `LIST_TASKS` definition, enforce a compatibility check that verifies:

- Optional priority/status admit null on the wire and normalize to omission.
- Required identifiers remain required; explicit required nullable values survive.
- Nested optional values, false, zero, and positional list values retain their
  specified meaning. Never strip every falsey value as a padding heuristic.
- Shared schemas are not mutated across providers.

Log strict-mode configuration and a schema digest alongside the existing runtime
stamp. Gate deployment on this contract using the installed runtime. Eventually
record the tested repository pair in a release manifest instead of deploying
two independently floating branches.

Strict mode provides a valid representation of absence. It cannot decide
whether “high priority” was actually requested; selection and evaluation still
need to address semantic mistakes.

## 2. Make task selection a complete domain operation

Evolve `list_tasks` to use a shared `TaskQuery` service. Keep its legacy arguments
compatible during migration. Do not add a competing fuzzy search tool.

The query contract needs:

- A scope: all tasks, daily view, scheduled on a specific user-local date,
  overdue, or set aside. Defaults remain open tasks; closed tasks require an
  explicit lifecycle filter.
- A title predicate with distinct literal `prefix`, `contains`, and `exact`
  operations. Case-insensitive matching and escaping are explicit; no model
  supplied regular expressions or SQL wildcards.
- Optional filters for status, priority, plan, and cycle. Absence means no
  restriction. A deliberate `unassigned` priority predicate means SQL NULL;
  NULL as an omitted argument does not mean “only unassigned.”
- Pagination for display, with `matched_total`, `returned_count`, and a cursor.
  All predicates execute before counting and limiting, including priority.

For daily scope, extract the existing bucketing into a shared service used by
the agenda, HTTP payload, and query tools. Open tasks in today's visible list
include scheduled-today, overdue, otherwise-in-progress, and set-aside rows.
Report those bucket counts separately. An `active_daily` scope excludes the
set-aside group; explicitly completed-today requests use the done-today predicate
with the user's timezone. Do not silently include completed tasks in an ordinary
archive request. Preserve the current in-progress treatment even for future
scheduled tasks until product semantics deliberately change.

Resolve relative dates on the server using the authenticated user's timezone.
Return the resolved date, timezone, scope, and applied filters. Pin that date for
the selection so execution over midnight does not change membership.

The model policy is: add only constraints expressed by the user or established
conversation context. “On today's list” means daily scope; “scheduled today”
means the date predicate. If two plausible readings select materially different
sets and the context does not resolve them, ask one concrete question. Do not
make users confirm every clear, reversible request.

### Structured search results

Initially serialize the result envelope as JSON in the existing string content
field. Include `outcome`, `applied_filters`, `matched_total`, `bucket_counts`,
`items`, `next_cursor`, and `selection_id` when executable.

For an empty result, return `outcome=empty`, not an exception. Provide bounded,
read-only diagnostics around a stable anchor such as the requested title:
counts by lifecycle, priority including unassigned, and date bucket. For this
incident, diagnostics could say “7 open prefix matches; all unassigned priority;
all carried over; none scheduled September 16.” These counts use the complete
candidate set, not the displayed page. This is more useful than dropping one
filter at a time when two independent filters exclude the set.

Alternative scopes are suggestions, never executable replacements for the
original selection. A failed query does not authorize widening its scope.
Validation failures, ambiguous names, and query-size limits have distinct codes
and suggested next steps. No SQL or raw internal exception details are needed.

### Freeze the entire selection

For a bounded result, store a short-lived opaque selection containing the owner,
conversation/request reference, normalized predicates, resolved date, complete
ordered task IDs, and relevant row versions or state fingerprints. The display
page can be 25 rows while the selection covers 70; explicitly report both.

Use a configurable batch ceiling, initially 500 as a proposed operating limit.
If exceeded, return `selection_too_large` and no executable token. Never create
a token for a silently truncated subset. A larger workflow can be introduced
later if actual usage warrants it.

Selection tokens are server-owned, unguessable, owner-checked, and expire, for
example after 15 minutes. They prove grounded membership, not user intent: the
model still has to choose a scope supported by the conversation. Tests must
measure wrong-scope behavior, not just valid token usage.

## 3. Archive the selected set in one transaction

Add `archive_tasks(selection_id)`. Its schema exposes no priority, schedule,
title, estimate, or arbitrary patch fields. An explicit ID selection can be
supported by the same selection service for “archive those three.”

Define archive as the product's existing “let it go” behavior:
`status=deprioritized`, correct `closed_at`, and `set_aside_on=null`. Keep task
history and links. It is neither task completion nor today's temporary set-aside.
The default selected population is open tasks; completed work is preserved unless
the user explicitly includes it.

Execution belongs in `WorkspaceStore`, reusing status and plan-touch invariants:

1. Check token owner, expiry, completeness, and operation replay state.
2. Acquire the appropriate row locks or conditional-write guards and revalidate
   the exact stored IDs. Never rerun a live title query to discover extra tasks.
3. Reject changed/deleted/unowned targets before any writes. Default to an
   all-or-nothing `selection_stale` outcome; a completed task must not silently
   become deprioritized because the selection was old.
4. Apply all transitions inside one database transaction. Refactor reusable
   in-session helpers; calling the current `update_task` N times would open N
   independent transactions and is not atomic.
5. Persist the operation receipt and audit data in the same transaction as the
   writes. Include before/after statuses, IDs, actor, request/selection identity,
   counts, and any relevant before/after park state.
6. Return the committed receipt. Replaying the same selection and operation
   returns that receipt, including after a lost response or process restart.

Serialize overlapping operations with database-level guards, not merely a
process-local lock. Two different selections sharing a row cannot both apply
against the same prior version. The replay lookup occurs before stale-version
checks so a successful retry does not reject its own previous changes.

Persisted receipts also permit a later exact undo operation: restore prior
state only if the row still matches the archive result. Retaining receipts is
part of the first implementation; adding an undo tool can be a subsequent slice.

The active focus clock is a separate concern: current desktop actions pause the
local sidecar before changing tasks. The server must not claim it stopped a
device-local timer. Include a client integration check that a closed running
task banks/pauses its run on refresh or sync; support delayed reconciliation for
offline devices. Archiving remains a workspace lifecycle operation.

## 4. Report outcomes from receipts

Introduce typed tool outcomes in dainframe, backward-compatible with handlers
returning strings. Distinguish `ok`, `empty`, `invalid_input`, `ambiguous`,
`selection_stale`, `selection_too_large`, and `failed`; mutation results include
`changed_count` and `operation_id`. Expected domain failures are visible to the
loop instead of all appearing successful because no exception was raised.

For the new archive path, derive completion count and status from the receipt.
A small application-rendered action summary is the strongest guarantee; persona
prose can accompany it but does not replace it. Do not present pre-tool claims
as completion evidence. The current loop collects intermediate text as well as
final text, so fixing only the last prompt would leave that hole.

Keep the five-round budget initially. Normally selection and archive take two
tool rounds followed by the reply. After an empty result, allow one informed
read-only diagnostic step, then either a supported correction or clarification;
do not walk the status/priority cross-product. The loop can enforce duplicate
normalized-query detection and a bounded no-progress count; product policy
determines which changed queries are justified.

At exhaustion, pass an explicit termination reason and the action ledger to
finalization. For this batch workflow, render a deterministic fallback from
receipts when necessary: “I couldn't resolve the matching set. No tasks were
archived.” If another operation committed, report its actual count instead.
Only describe missing permissions or unavailable capabilities when an actual
capability/error result supports that claim. Never infer it from zero matches.

Record bounded diagnostics in traces: raw versus normalized arguments where
appropriate, applied query, matched count, outcome code, changed count,
operation ID, and cap reason. Existing traces record call inputs and `is_error`
but not enough result semantics to diagnose these failures directly.

## 5. Expected behavior for the reported request

If context establishes “daily tasks” as the daily list:

```text
list_tasks(scope="daily", title_prefix="Pomodoro #")
  -> 7 open matches, all carried over, selection_id=S
archive_tasks(selection_id=S)
  -> committed receipt, changed_count=7
reply: Archived the 7 open “Pomodoro #…” tasks from your daily list.
```

Those counts are illustrative from the reported dataset; they are not a fresh
production query. A completed eighth task remains completed. Unrelated titles
containing Pomodoro elsewhere remain unchanged.

If the user explicitly says “scheduled today,” preserve that restriction. With
zero matches and seven older ones, ask “None are scheduled today; do you mean
the seven carried-over Pomodoro tasks?” Do not archive them before the answer.

## 6. Delivery and acceptance

Implement in three reviewable slices:

1. **Runtime contract and accurate queries:** verify/deploy existing dainframe
   fix; query-before-limit; literal prefix; counts/cursors; shared daily scopes;
   structured diagnostic results. Add tests using Chordial's actual tool schema
   through the provider adapter, not only synthetic schemas.
2. **Selection and archive:** selection persistence, transaction-safe store
   operation, durable receipts/replay, registrations and persona allowlists,
   migration, and client focus-clock integration coverage.
3. **Truthful loop and release evaluation:** typed outcomes, bounded recovery,
   receipt-based finalization, enriched traces, and full conversational tests.

Retain the narrow architecture in `ACTION_RECONCILIATION_ENGINE.md`: explicit
commands stay in the persona tool loop and operations converge in the store.
Do not expand the implicit completion reconciler into a second archive writer.

Required cases:

- Reported seven-open/one-done dataset, NULL and mixed priorities, blank/null
  padding, To do and In progress, exact-date versus daily-list wording.
- Prefix distractors; literal `%`, `_`, `#`; duplicate names; empty selectors;
  intentionally unassigned priority; malformed dates; timezone midnight/DST.
- More than 25 matches, beyond-page priority matches, and over-ceiling selection.
- Set-aside and future/in-progress bucket behavior identical across UI and chat.
- Other users' IDs/tokens, expired selections, concurrent edits and completion,
  overlapping batches, restart/lost-response retry, and transaction rollback.
- Cap exhaustion before writes and after a committed write; no false completion,
  no fabricated “cannot archive” claim, and no loss of the receipt/audit trail.
- Foreground and offline device behavior when the archived task has a live run.

Use deterministic tests for query/store/provider invariants and scripted model
responses for loop behavior. Also run repeated real-model conversations against
disposable workspaces on the deployed model/configuration. Assert final database
state and unchanged unrelated fields, not specific assistant wording or merely
whether tools were called. Include ambiguous requests whose correct result is
clarification with zero writes. Compare baseline and proposal on the same cases;
report completion, wrong-target writes, clarification rate, rounds, and latency.
Proposed release bar: zero wrong-target writes in the regression corpus and at
least 95% completion on clear supported requests across repeated trials. This is
a target to measure, not an already-demonstrated reliability claim.

## Verification performed for this design

- Existing workspace tool suite: 27 passed.
- Existing dainframe OpenAI provider suite: 14 passed.
- Disposable SQLite reproduction: seven open unassigned-priority tasks match
  without added filters; each priority filter and today's exact date returns
  empty. A high-priority match after the first row disappears with `limit=1`
  and appears with `limit=25`, confirming filter-after-limit behavior.
- Local imported provider emits strict mode, nullable priority type/enum, and
  removes optional null priority on normalization. No live model or production
  writes were used. Existing suites do not establish conversational reliability.

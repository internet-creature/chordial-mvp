// stuck mode, the page's pure half (docs/STUCK_MODE_DESIGN.md §1-§2, §4):
// the copy, the request that crosses windows, which card to show, the
// thinking lines, the hide-the-why preference, and the accept-time handoff.
// no fetch, no tauri - everything here is unit-testable.

import type {
  StuckEpisode,
  StuckExecution,
  StuckKind,
  StuckProposal,
  StuckSurface,
} from "../api/types";

/** the authored copy, one place for the voice pass */
export const STUCK_COPY = {
  button: "i’m stuck",
  barButton: "stuck?",
  buttonTitle: "press it. that’s the whole ask.",
  thinkingHead: "formulating a strategy for getting you unstuck",
  differentAngleHead: "okay. different angle.",
  thinkingLines: [
    "reading the day so far…",
    "looking at what’s on the list…",
    "finding the smallest thing…",
    "checking what worked last time…",
    "almost.",
  ],
  fallbackHead: "i couldn’t reach the house, so here’s the plainest thing i’ve got.",
  failedHead: "the house didn’t answer.",
  cardIntro: "let’s make this easier.",
  doThis: "do this ▸",
  different: "something different",
  tooMuch: "that’s too much",
  hideWhy: "hide the why",
  showWhy: "show the why",
  restLine: "okay. you don’t have to look at the list right now.",
  restLineTwo: "nothing changed. it can wait outside this room.",
  restSleep: "sleep is the move. the list will be exactly where you left it.",
  close: "close",
  oneSmallThing: "actually, one small thing",
  askAgain: "ask again",
  tryHouseAgain: "try the house again",
  startedLine: "clock’s running in the companion. i’m right there.",
  stopHere: "stop here ✓",
  keepGoing: "keep going",
  boundaryTitle: "the container’s full — both are real endings",
  resumeLine: "the first piece exists now. next time doesn’t start from blank.",
  startIt: "start it ▸",
  startFailed: "the companion didn’t answer — the step is still yours to start",
  startSpent: "that step already ran.",
  chosenLine: "you already chose. the companion has it.",
  enoughPrefix: "enough = ",
  container: (minutes: number) => `${minutes} min · then you choose`,
  awayContainer: (minutes: number) => `${minutes} min away · the step waits`,
  justSitting: "just sitting",
  errorOpen: "couldn’t reach the house — try again",
  errorReact: "that didn’t land — try again",
  stale: "the card changed — here’s the fresh one",
} as const;

/** the mono eyebrow above the line: what sort of help this is */
export const KIND_EYEBROW: Record<StuckKind, string> = {
  step: "one small step",
  switch: "a different thing",
  thread: "pick up the thread",
  body: "body first",
  sound: "something to hear",
  rest: "rest",
  company: "company",
};

// --- the request that crosses windows ------------------------------------------
// the companion (and the tray, via the shell) can't render the page; they
// write the press to localStorage and raise the main window, which reads it
// on the storage event or at mount (the house's cross-window convention -
// see lib/taskSync.ts). the key is cleared on read so a press never replays.

export const STUCK_REQUEST_KEY = "chordial.stuck.request";

export interface StuckRequest {
  request_id: string;
  surface: StuckSurface;
  task_id: number | null;
  at: number;
}

export function newRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  // a browser preview without crypto.randomUUID: still a valid v4 shape
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function makeRequest(
  surface: StuckSurface,
  taskId: number | null = null,
  now: number = Date.now(),
): StuckRequest {
  return { request_id: newRequestId(), surface, task_id: taskId, at: now };
}

export function writeStuckRequest(
  storage: Storage,
  request: StuckRequest,
): void {
  try {
    storage.setItem(STUCK_REQUEST_KEY, JSON.stringify(request));
  } catch {
    // storage unavailable: the press is lost, the button is still there
  }
}

/** read AND clear: a press is delivered once. stale presses (older than
 * a minute - a main window that wasn't running) are dropped, not replayed
 * onto a person who has since moved on. */
export function readStuckRequest(
  storage: Storage,
  now: number = Date.now(),
  maxAgeMs: number = 60_000,
): StuckRequest | null {
  let raw: string | null = null;
  try {
    raw = storage.getItem(STUCK_REQUEST_KEY);
    if (raw !== null) storage.removeItem(STUCK_REQUEST_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<StuckRequest>;
    if (
      typeof parsed.request_id !== "string" ||
      typeof parsed.surface !== "string" ||
      typeof parsed.at !== "number"
    ) {
      return null;
    }
    if (now - parsed.at > maxAgeMs) return null;
    return {
      request_id: parsed.request_id,
      surface: parsed.surface as StuckSurface,
      task_id: typeof parsed.task_id === "number" ? parsed.task_id : null,
      at: parsed.at,
    };
  } catch {
    return null;
  }
}

// --- hide the why (§2.5): a preference, not a per-episode toggle ---------------

export const HIDE_WHY_KEY = "chordial.stuck.hide_why";

export function whyHidden(storage: Storage): boolean {
  try {
    return storage.getItem(HIDE_WHY_KEY) === "1";
  } catch {
    return false;
  }
}

export function setWhyHidden(storage: Storage, hidden: boolean): void {
  try {
    if (hidden) storage.setItem(HIDE_WHY_KEY, "1");
    else storage.removeItem(HIDE_WHY_KEY);
  } catch {
    // a preference that can't be saved is asked again next time
  }
}

// --- which card (§2.2-§2.3) ----------------------------------------------------

/** the one proposal to show: the first of the current generation the
 * person hasn't turned down. null while thinking, or when every card of
 * this generation is gone (the client shows rest). */
export function visibleProposal(
  episode: StuckEpisode | null,
): StuckProposal | null {
  if (!episode) return null;
  if (episode.status !== "ready" && episode.status !== "fallback") return null;
  const rejected = new Set(episode.rejected_proposal_ids);
  return (
    episode.proposals.find(
      (p) => p.generation === episode.generation && !rejected.has(p.proposal_id),
    ) ?? null
  );
}

/** the rest proposal of the current generation, for its evidence (the
 * sleep line shows only when the ladder or the turn cited the night) */
export function restEvidence(episode: StuckEpisode | null): string[] {
  if (!episode) return [];
  const rest = episode.proposals.find(
    (p) => p.generation === episode.generation && p.kind === "rest",
  );
  return rest?.rationale_codes ?? [];
}

export const SLEEP_CODES = ["past_quiet_hours", "late_last_night"] as const;

export function suggestsSleep(codes: readonly string[]): boolean {
  return codes.some((c) => (SLEEP_CODES as readonly string[]).includes(c));
}

/** the thinking sub-line rotates every few seconds; the last one holds */
export function thinkingLine(tick: number): string {
  const lines = STUCK_COPY.thinkingLines;
  return lines[Math.min(Math.max(0, tick), lines.length - 1)];
}

export const THINKING_LINE_MS = 3000;
export const POLL_MS = 1500;

/** "enough = …" once, whatever the turn wrote */
export function enoughLine(enough: string): string {
  const prefix = STUCK_COPY.enoughPrefix;
  const clean = enough.trim();
  return clean.toLowerCase().startsWith(prefix) ? clean : prefix + clean;
}

/** the chip under the line: the task the proposal belongs to */
export function thingLabel(
  proposal: StuckProposal,
  titles: ReadonlyMap<number, string>,
): string | null {
  const thing = proposal.thing;
  if (!thing) return null;
  if (typeof thing.task_id === "number") {
    return titles.get(thing.task_id) ?? "a task on today’s list";
  }
  if (typeof thing.label === "string" && thing.label) return thing.label;
  return null;
}

export function containerLabel(proposal: StuckProposal): string | null {
  if (proposal.minutes === null) return null;
  return proposal.action === "pause_and_away"
    ? STUCK_COPY.awayContainer(proposal.minutes)
    : STUCK_COPY.container(proposal.minutes);
}

// --- the accept-time handoff (§4) -------------------------------------------------
// a start carries the execution the sidecar deduplicates on (the same
// accepted proposal can never start a second block); everything else shows
// its line and closes.

export interface HandoffExecution {
  execution_id: string;
  episode_id: string;
}

export type Handoff =
  | {
      kind: "start";
      taskId: number | null;
      label: string;
      minutes: number;
      execution: HandoffExecution;
    }
  | { kind: "line"; line: string };

export function handoffFor(
  execution: StuckExecution,
  titles: ReadonlyMap<number, string>,
): Handoff {
  if (execution.action === "start_task" && execution.task_id !== null) {
    const title = titles.get(execution.task_id) ?? "a task";
    const scope = execution.next_action?.trim() || null;
    return {
      kind: "start",
      taskId: execution.task_id,
      label: scope ? `${title}: ${scope}` : title,
      minutes: execution.minutes ?? 2,
      execution: {
        execution_id: execution.execution_id,
        episode_id: execution.episode_id,
      },
    };
  }
  if (execution.action === "start_company") {
    return {
      kind: "start",
      taskId: null,
      label: STUCK_COPY.justSitting,
      minutes: execution.minutes ?? 5,
      execution: {
        execution_id: execution.execution_id,
        episode_id: execution.episode_id,
      },
    };
  }
  return { kind: "line", line: execution.line };
}

// --- the boundary (§4) -------------------------------------------------------------
// the resume point is the server's to write, from the durable session.ended
// (a dead network can't lose it); the window only says the evidence line.

/** the boundary shows on a stuck run once it is over target, until the
 * person chooses - the choice persists with the run in the sidecar, so a
 * reload never asks again. an open rewind question takes the stage instead. */
export function boundaryShowing(args: {
  running: boolean;
  runMode: string | null | undefined;
  overtime: boolean;
  boundaryChoice: string | null | undefined;
  questionOpen: boolean;
}): boolean {
  return (
    args.running &&
    args.runMode === "stuck" &&
    args.overtime &&
    !args.questionOpen &&
    !args.boundaryChoice
  );
}

/** task id -> title, from today's buckets, for the chips and the label */
export function titleMap(
  buckets: Record<string, Array<{ id: number; title: string }>> | undefined,
): Map<number, string> {
  const out = new Map<number, string>();
  if (!buckets) return out;
  for (const rows of Object.values(buckets)) {
    for (const row of rows) out.set(row.id, row.title);
  }
  return out;
}

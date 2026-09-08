// the scope and the parked row (docs/FOCUS_DOGFOOD_DESIGN.md §2-§3): pure
// helpers for the companion window. the authored copy lives HERE so a
// voice pass replaces strings in one place.

/** the scope line's cap - the server trims to the same length */
export const SCOPE_CAP = 140;

/** the run label convention (§3): `title` when unscoped, otherwise
 * `"<title>: <scope>"` - pip's "landed <label>" observation and the cycle
 * view inherit the scope for free. */
export function runLabel(title: string, scope: string | null | undefined): string {
  const clean = (scope ?? "").trim();
  return clean ? `${title}: ${clean}` : title;
}

/** the inverse, done structurally: task titles are unrestricted (a title
 * may itself contain ": "), so the split needs the canonical title of
 * the task the clock runs on. with it, the scope is exactly what follows
 * `"<title>: "`; without it (the task isn't in today's lists) nothing is
 * guessed - the whole label renders as the title. */
export function splitLabel(
  label: string,
  title?: string | null,
): { title: string; scope: string | null } {
  if (title) {
    if (label === title) return { title, scope: null };
    const prefix = `${title}: `;
    if (label.startsWith(prefix)) {
      return { title, scope: label.slice(prefix.length) || null };
    }
  }
  return { title: label, scope: null };
}

/** "just start" is remembered per task per local day - a per-viewer
 * convenience, never authoritative; tomorrow the form asks again */
export const scopeSkipKey = (taskId: number, dateIso: string) =>
  `chordial.scope_skipped:${taskId}:${dateIso}`;

export function scopeSkipped(
  storage: Storage,
  taskId: number,
  dateIso: string,
): boolean {
  try {
    return storage.getItem(scopeSkipKey(taskId, dateIso)) === "1";
  } catch {
    return false;
  }
}

export function rememberScopeSkip(
  storage: Storage,
  taskId: number,
  dateIso: string,
): void {
  try {
    storage.setItem(scopeSkipKey(taskId, dateIso), "1");
  } catch {
    // storage may be unavailable; the form simply asks again
  }
}

/** the calendar day after an ISO date, as an ISO date - string math on
 * the user's local day, so no timezone can shift it */
export function tomorrowOf(dateIso: string): string {
  const [y, m, d] = dateIso.split("-").map(Number);
  const next = new Date(Date.UTC(y, m - 1, d + 1));
  return next.toISOString().slice(0, 10);
}

/** the authored copy, deer-voiced; one place for the voice pass */
export const SCOPE_COPY = {
  prompt: "what’s the first piece?",
  placeholder: "one line…",
  start: "start",
  justStart: "just start",
  editScope: "change the first piece",
  setAside: "set aside for today",
  setAsideRunning: "pause & set aside",
  asideHeading: "set aside",
  tomorrow: "tomorrow",
  bringBack: "bring back",
  letGo: "let it go",
  asideNote: "parked for today — no pressure, it comes back tomorrow",
} as const;

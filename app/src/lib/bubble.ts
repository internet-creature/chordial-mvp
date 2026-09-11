// the companion's speech bubble carries only what the companion said: an
// authored line from the sidecar. it never times out; it leaves when the
// next line arrives or the person dismisses it, and it survives a form
// switch, a reload, or a reopened window through session storage. the
// day's counts and states are chrome, and go in the status row instead.

export interface DayShape {
  blocked: boolean;
  drifting: boolean;
  running: boolean;
  overtime: boolean;
  openCount: number;
  doneCount: number;
  minutesToday: number;
}

/** the status row's words, one place for the copy pass */
export const STATUS_COPY = {
  hushed: "notifications paused",
  drifting: "away from desk",
  overtime: "past target",
  empty: "no tasks today",
} as const;

/** the day as quantities: counts, and the state that qualifies them */
export function dayStatus(day: DayShape): string {
  const state = day.blocked
    ? STATUS_COPY.hushed
    : day.drifting
      ? STATUS_COPY.drifting
      : day.running && day.overtime
        ? STATUS_COPY.overtime
        : null;
  if (day.openCount === 0 && day.doneCount === 0 && day.minutesToday === 0) {
    return state ? `${state} · ${STATUS_COPY.empty}` : STATUS_COPY.empty;
  }
  const bits = [`${day.openCount} to go`];
  if (day.doneCount > 0) bits.push(`${day.doneCount} done`);
  if (day.minutesToday > 0) bits.push(`${day.minutesToday} min today`);
  if (state) bits.unshift(state);
  return bits.join(" · ");
}

/** what the companion last said. `seq` orders sayings and lets the bar
 * tell a fresh one from an old one; a dismissed saying keeps its seq so a
 * remount does not bring it back. */
export interface Saying {
  seq: number;
  text: string;
}

const SAYING_KEY = "chordial.companion.saying";

interface StoredSaying extends Saying {
  dismissed?: boolean;
}

export function loadSaying(storage: Storage | null): Saying | null {
  try {
    const raw = storage?.getItem(SAYING_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredSaying>;
    if (
      typeof parsed.seq !== "number" ||
      typeof parsed.text !== "string" ||
      parsed.text === "" ||
      parsed.dismissed
    ) {
      return null;
    }
    return { seq: parsed.seq, text: parsed.text };
  } catch {
    return null;
  }
}

export function saveSaying(storage: Storage | null, saying: Saying): void {
  try {
    storage?.setItem(SAYING_KEY, JSON.stringify(saying));
  } catch {
    // the saying still shows for this session
  }
}

/** the text of the last saying whether or not it was dismissed: the
 * sidecar repeats its latest line on every connect, and a repeat is not
 * a new saying */
export function lastSayingText(storage: Storage | null): string | null {
  try {
    const raw = storage?.getItem(SAYING_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredSaying>;
    return typeof parsed.text === "string" ? parsed.text : null;
  } catch {
    return null;
  }
}

/** a dismissed saying stays dismissed: the entry is kept, marked */
export function dismissSaying(storage: Storage | null): void {
  try {
    const raw = storage?.getItem(SAYING_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as StoredSaying;
    storage?.setItem(
      SAYING_KEY,
      JSON.stringify({ ...parsed, dismissed: true }),
    );
  } catch {
    // nothing to keep
  }
}

/** a new saying: later than any before it, even within the same tick */
export function nextSeq(previous: Saying | null, now: number): number {
  return previous ? Math.max(previous.seq + 1, now) : now;
}

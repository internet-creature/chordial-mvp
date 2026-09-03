// the companion window's forms and small remembered choices
// (docs/FOCUS_DOGFOOD_DESIGN.md §11): pure helpers, storage-injected so
// they test without a browser. the window itself is driven from
// DeerWindow via lib/tauriWindow.

/** den = the full window (deer, tasks, quick-add); bar = the slim strip
 * that sits with you while a clock runs */
export type Form = "den" | "bar";

export interface Size {
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

/** logical (css) pixel sizes; the den matches tauri.conf's deer window */
export const FORM_SIZES: Record<Form, Size> = {
  den: { width: 270, height: 500 },
  bar: { width: 440, height: 56 },
};

/** the block-target chips, minutes */
export const TARGET_CHOICES: readonly number[] = [10, 25, 50];

export const AUTO_BAR_KEY = "chordial.companion.auto_bar";
const POSITION_PREFIX = "chordial.companion.pos:";
const TARGET_PREFIX = "chordial.companion.target:";

/** which form the window should wear right now. the bar is for a
 * running clock; "peeking" is the person having opened the den from the
 * bar without stopping the clock - it holds until the clock stops or
 * they go back. auto-bar off = always the den. */
export function formFor(args: {
  running: boolean;
  autoBar: boolean;
  peeking: boolean;
}): Form {
  return args.running && args.autoBar && !args.peeking ? "bar" : "den";
}

/** den → bar on block start, unless turned off (default on) */
export function autoBarEnabled(storage: Storage): boolean {
  try {
    return storage.getItem(AUTO_BAR_KEY) !== "0";
  } catch {
    return true;
  }
}

export function setAutoBar(storage: Storage, on: boolean): void {
  try {
    if (on) storage.removeItem(AUTO_BAR_KEY);
    else storage.setItem(AUTO_BAR_KEY, "0");
  } catch {
    // a convenience, never a requirement
  }
}

/** each form remembers its own place on the screen (the window-state
 * plugin keeps one rect per window, so this is ours to keep) */
export function loadFormPosition(storage: Storage, form: Form): Point | null {
  try {
    const raw = storage.getItem(POSITION_PREFIX + form);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed.x === "number" &&
      typeof parsed.y === "number" &&
      Number.isFinite(parsed.x) &&
      Number.isFinite(parsed.y)
    ) {
      return { x: parsed.x, y: parsed.y };
    }
    return null;
  } catch {
    return null;
  }
}

export function saveFormPosition(
  storage: Storage,
  form: Form,
  point: Point,
): void {
  try {
    storage.setItem(POSITION_PREFIX + form, JSON.stringify(point));
  } catch {
    // ignore
  }
}

/** where the bar goes the first time, given where the den was: along
 * the den's bottom edge, so the strip appears where the deer's feet
 * were instead of jumping to the den's top-left */
export function barPositionFrom(den: Point, denSize: Size, barSize: Size): Point {
  return { x: den.x, y: Math.max(0, den.y + denSize.height - barSize.height) };
}

/** the inverse: the den grows upward from the bar's bottom edge, never
 * above the top of the screen */
export function denPositionFrom(bar: Point, denSize: Size, barSize: Size): Point {
  return { x: bar.x, y: Math.max(0, bar.y + barSize.height - denSize.height) };
}

/** the last target chosen for a task (this session), else the fallback */
export function lastTarget(
  storage: Storage,
  taskId: number,
  fallback: number,
): number {
  try {
    const raw = storage.getItem(TARGET_PREFIX + taskId);
    const minutes = raw === null ? NaN : Number(raw);
    return Number.isFinite(minutes) && minutes > 0 && minutes <= 120
      ? minutes
      : fallback;
  } catch {
    return fallback;
  }
}

export function rememberTarget(
  storage: Storage,
  taskId: number,
  minutes: number,
): void {
  try {
    storage.setItem(TARGET_PREFIX + taskId, String(minutes));
  } catch {
    // ignore
  }
}

/** the chips to offer: the standard three, plus the house default if it
 * isn't one of them, ascending */
export function targetChoices(pomMinutes: number): number[] {
  const set = new Set<number>(TARGET_CHOICES);
  if (Number.isFinite(pomMinutes) && pomMinutes > 0) set.add(pomMinutes);
  return [...set].sort((a, b) => a - b);
}

const FORM_KEY = "chordial.companion.form";

/** the form the window last wore - the window-state plugin restores
 * whatever rect was current at exit, so a launch after a bar-form exit
 * must know to grow back into the den */
export function loadLastForm(storage: Storage): Form | null {
  try {
    const raw = storage.getItem(FORM_KEY);
    return raw === "den" || raw === "bar" ? raw : null;
  } catch {
    return null;
  }
}

export function saveLastForm(storage: Storage, form: Form): void {
  try {
    storage.setItem(FORM_KEY, form);
  } catch {
    // ignore
  }
}

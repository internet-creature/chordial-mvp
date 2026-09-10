// the deer's speech bubble (docs/FOCUS_DOGFOOD_DESIGN.md §11, dogfood
// 2026-09-08): always present, never a pop-in. it carries the latest
// authored line when there is one; otherwise a quiet, factual line about
// the day - data, not voice, so the voice pass owns every other word.

export interface DayShape {
  blocked: boolean;
  drifting: boolean;
  running: boolean;
  overtime: boolean;
  openCount: number;
  doneCount: number;
  minutesToday: number;
}

/** the fallback strings, one place for the voice pass */
export const BUBBLE_COPY = {
  hushed: "notifications paused",
  drifting: "away from desk",
  overtime: "past target",
  empty: "no tasks scheduled",
} as const;

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** what the bubble says when no authored line is showing */
export function bubbleFallback(day: DayShape): string {
  if (day.blocked) return BUBBLE_COPY.hushed;
  if (day.drifting) return BUBBLE_COPY.drifting;
  if (day.running && day.overtime) return BUBBLE_COPY.overtime;
  if (day.openCount === 0 && day.doneCount === 0 && day.minutesToday === 0) {
    return BUBBLE_COPY.empty;
  }
  const bits = [plural(day.openCount, "to go", "to go")];
  if (day.doneCount > 0) bits.push(`${day.doneCount} done`);
  if (day.minutesToday > 0) bits.push(`${day.minutesToday} min today`);
  return bits.join(" · ");
}

// what the room shows while a reply is on its way. plain, brief, and
// picked at random, never the same phrase twice in a row.

export const STIRRINGS: string[] = [
  "thinking",
  "reading back",
  "checking notes",
  "drafting a reply",
  "thinking it over",
  "gathering context",
  "choosing words",
  "reviewing the day",
];

/** a random stirring, never `current` (so the rotation always visibly moves) */
export function pickStirring(current?: string): string {
  const pool = STIRRINGS.filter((s) => s !== current);
  return pool[Math.floor(Math.random() * pool.length)];
}

import { describe, expect, it } from "vitest";
import { BUBBLE_COPY, bubbleFallback } from "./bubble";

const quiet = {
  blocked: false,
  drifting: false,
  running: false,
  overtime: false,
  openCount: 0,
  doneCount: 0,
  minutesToday: 0,
};

describe("the bubble's fallback", () => {
  it("names an empty day", () => {
    expect(bubbleFallback(quiet)).toBe(BUBBLE_COPY.empty);
  });
  it("counts the day, data not voice", () => {
    expect(bubbleFallback({ ...quiet, openCount: 3 })).toBe("3 to go");
    expect(
      bubbleFallback({ ...quiet, openCount: 2, doneCount: 1, minutesToday: 42 }),
    ).toBe("2 to go · 1 done · 42 min today");
    expect(bubbleFallback({ ...quiet, doneCount: 2, minutesToday: 50 })).toBe(
      "0 to go · 2 done · 50 min today",
    );
  });
  it("the room's state wins over the count", () => {
    expect(bubbleFallback({ ...quiet, openCount: 3, blocked: true })).toBe(
      BUBBLE_COPY.hushed,
    );
    expect(bubbleFallback({ ...quiet, openCount: 3, drifting: true })).toBe(
      BUBBLE_COPY.drifting,
    );
    expect(
      bubbleFallback({ ...quiet, openCount: 3, running: true, overtime: true }),
    ).toBe(BUBBLE_COPY.overtime);
    // overtime only means something while a clock runs
    expect(bubbleFallback({ ...quiet, openCount: 3, overtime: true })).toBe(
      "3 to go",
    );
  });
});

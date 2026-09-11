import { describe, expect, it } from "vitest";
import {
  STATUS_COPY,
  dayStatus,
  dismissSaying,
  lastSayingText,
  loadSaying,
  nextSeq,
  saveSaying,
} from "./bubble";

const quiet = {
  blocked: false,
  drifting: false,
  running: false,
  overtime: false,
  openCount: 0,
  doneCount: 0,
  minutesToday: 0,
};

describe("the status row", () => {
  it("names an empty day", () => {
    expect(dayStatus(quiet)).toBe(STATUS_COPY.empty);
  });
  it("counts the day as quantities", () => {
    expect(dayStatus({ ...quiet, openCount: 3 })).toBe("3 to go");
    expect(
      dayStatus({ ...quiet, openCount: 2, doneCount: 1, minutesToday: 42 }),
    ).toBe("2 to go · 1 done · 42 min today");
    expect(dayStatus({ ...quiet, doneCount: 2, minutesToday: 50 })).toBe(
      "0 to go · 2 done · 50 min today",
    );
  });
  it("a state qualifies the count rather than replacing it", () => {
    expect(dayStatus({ ...quiet, openCount: 3, blocked: true })).toBe(
      `${STATUS_COPY.hushed} · 3 to go`,
    );
    expect(dayStatus({ ...quiet, openCount: 3, drifting: true })).toBe(
      `${STATUS_COPY.drifting} · 3 to go`,
    );
    expect(
      dayStatus({ ...quiet, openCount: 3, running: true, overtime: true }),
    ).toBe(`${STATUS_COPY.overtime} · 3 to go`);
    expect(dayStatus({ ...quiet, blocked: true })).toBe(
      `${STATUS_COPY.hushed} · ${STATUS_COPY.empty}`,
    );
    // overtime only means something while a clock runs
    expect(dayStatus({ ...quiet, openCount: 3, overtime: true })).toBe(
      "3 to go",
    );
  });
});

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k) => map.get(k) ?? null,
    key: (i) => [...map.keys()][i] ?? null,
    removeItem: (k) => void map.delete(k),
    setItem: (k, v) => void map.set(k, String(v)),
  };
}

describe("the last saying", () => {
  it("is state: saved, loaded back, and gone once dismissed", () => {
    const s = memoryStorage();
    expect(loadSaying(s)).toBeNull();
    saveSaying(s, { seq: 5, text: "clock's yours." });
    expect(loadSaying(s)).toEqual({ seq: 5, text: "clock's yours." });
    dismissSaying(s);
    expect(loadSaying(s)).toBeNull();
    // a connect that repeats the dismissed line must not revive it
    expect(lastSayingText(s)).toBe("clock's yours.");
    // a later saying replaces the dismissed one
    saveSaying(s, { seq: 6, text: "paused!" });
    expect(loadSaying(s)).toEqual({ seq: 6, text: "paused!" });
  });
  it("ignores a malformed or empty entry", () => {
    const s = memoryStorage();
    s.setItem("chordial.companion.saying", "{not json");
    expect(loadSaying(s)).toBeNull();
    s.setItem("chordial.companion.saying", JSON.stringify({ seq: 1, text: "" }));
    expect(loadSaying(s)).toBeNull();
    expect(() => dismissSaying(s)).not.toThrow();
    expect(loadSaying(null)).toBeNull();
  });
  it("orders sayings even within one tick", () => {
    expect(nextSeq(null, 100)).toBe(100);
    expect(nextSeq({ seq: 100, text: "a" }, 100)).toBe(101);
    expect(nextSeq({ seq: 100, text: "a" }, 500)).toBe(500);
  });
});

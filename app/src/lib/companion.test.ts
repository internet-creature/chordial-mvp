import { describe, expect, it } from "vitest";
import {
  autoBarEnabled,
  barPositionFrom,
  clampToArea,
  denPositionFrom,
  FORM_SIZES,
  formFor,
  lastTarget,
  loadFormPosition,
  rememberTarget,
  saveFormPosition,
  setAutoBar,
  targetChoices,
} from "./companion";

function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
    clear: () => map.clear(),
    key: () => null,
    get length() {
      return map.size;
    },
  } as Storage;
}

describe("the companion's form", () => {
  it("wears the bar only for a running clock with auto-bar on", () => {
    expect(formFor({ running: true, autoBar: true, peeking: false })).toBe("bar");
    expect(formFor({ running: false, autoBar: true, peeking: false })).toBe("den");
    expect(formFor({ running: true, autoBar: false, peeking: false })).toBe("den");
  });

  it("peeking opens the den without stopping the clock", () => {
    expect(formFor({ running: true, autoBar: true, peeking: true })).toBe("den");
  });

  it("auto-bar defaults on, turns off once, and back", () => {
    const s = fakeStorage();
    expect(autoBarEnabled(s)).toBe(true);
    setAutoBar(s, false);
    expect(autoBarEnabled(s)).toBe(false);
    setAutoBar(s, true);
    expect(autoBarEnabled(s)).toBe(true);
  });
});

describe("per-form positions", () => {
  it("round-trip and ignore garbage", () => {
    const s = fakeStorage();
    expect(loadFormPosition(s, "bar")).toBeNull();
    saveFormPosition(s, "bar", { x: 12, y: 800 });
    expect(loadFormPosition(s, "bar")).toEqual({ x: 12, y: 800 });
    expect(loadFormPosition(s, "den")).toBeNull();
    s.setItem("chordial.companion.pos:den", "{not json");
    expect(loadFormPosition(s, "den")).toBeNull();
    s.setItem("chordial.companion.pos:den", JSON.stringify({ x: "1", y: 2 }));
    expect(loadFormPosition(s, "den")).toBeNull();
  });

  it("the bar keeps the den's right and bottom edges, and the den grows back", () => {
    // a den placed 16px from the right of a 1440-wide screen: the wider
    // bar must not run off the right edge (sol's #84 round)
    const den = { x: 1440 - 270 - 16, y: 300 };
    const bar = barPositionFrom(den, FORM_SIZES.den, FORM_SIZES.bar);
    expect(bar).toEqual({ x: 1440 - 440 - 16, y: 300 + 500 - 56 });
    expect(bar.x + FORM_SIZES.bar.width).toBe(den.x + FORM_SIZES.den.width);
    expect(denPositionFrom(bar, FORM_SIZES.den, FORM_SIZES.bar)).toEqual(den);
  });

  it("never places a form above or left of the origin", () => {
    expect(denPositionFrom({ x: 0, y: 20 }, FORM_SIZES.den, FORM_SIZES.bar).y).toBe(0);
    expect(barPositionFrom({ x: 10, y: 0 }, FORM_SIZES.den, FORM_SIZES.bar).x).toBe(0);
    expect(
      barPositionFrom({ x: 0, y: 0 }, { width: 10, height: 10 }, FORM_SIZES.bar),
    ).toEqual({ x: 0, y: 0 });
  });

  it("clamps a window into the work area", () => {
    const area = { x: 0, y: 25, width: 1440, height: 875 };
    // off the right edge and below the bottom: pulled back in
    expect(clampToArea({ x: 1300, y: 900 }, FORM_SIZES.bar, area)).toEqual({
      x: 1000,
      y: 844,
    });
    // above the menu bar: pushed down to the area's top
    expect(clampToArea({ x: 40, y: 0 }, FORM_SIZES.den, area)).toEqual({ x: 40, y: 25 });
    // already inside: untouched
    expect(clampToArea({ x: 40, y: 100 }, FORM_SIZES.den, area)).toEqual({ x: 40, y: 100 });
    // a window larger than the area pins to the origin instead of NaN-ing
    expect(clampToArea({ x: 5, y: 5 }, { width: 2000, height: 2000 }, area)).toEqual({
      x: 0,
      y: 25,
    });
  });
});

describe("block targets", () => {
  it("remembers the last target per task and falls back otherwise", () => {
    const s = fakeStorage();
    expect(lastTarget(s, 7, 25)).toBe(25);
    rememberTarget(s, 7, 50);
    expect(lastTarget(s, 7, 25)).toBe(50);
    expect(lastTarget(s, 8, 25)).toBe(25);
    s.setItem("chordial.companion.target:7", "nope");
    expect(lastTarget(s, 7, 25)).toBe(25);
    s.setItem("chordial.companion.target:7", "999");
    expect(lastTarget(s, 7, 25)).toBe(25);
  });

  it("offers the house default alongside the standard chips", () => {
    expect(targetChoices(25)).toEqual([10, 25, 50]);
    expect(targetChoices(15)).toEqual([10, 15, 25, 50]);
    expect(targetChoices(NaN)).toEqual([10, 25, 50]);
  });
});

describe("the last form worn", () => {
  it("round-trips and rejects anything else", async () => {
    const { loadLastForm, saveLastForm } = await import("./companion");
    const s = fakeStorage();
    expect(loadLastForm(s)).toBeNull();
    saveLastForm(s, "bar");
    expect(loadLastForm(s)).toBe("bar");
    s.setItem("chordial.companion.form", "tent");
    expect(loadLastForm(s)).toBeNull();
  });
});

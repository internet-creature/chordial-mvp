import { describe, expect, it } from "vitest";
import {
  rememberScopeSkip,
  runLabel,
  scopeSkipped,
  SCOPE_CAP,
  splitLabel,
  tomorrowOf,
} from "./scope";

class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length() {
    return this.map.size;
  }
  clear() {
    this.map.clear();
  }
  getItem(k: string) {
    return this.map.get(k) ?? null;
  }
  key(i: number) {
    return [...this.map.keys()][i] ?? null;
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
  setItem(k: string, v: string) {
    this.map.set(k, v);
  }
}

describe("the run label convention", () => {
  it("is the title alone when unscoped", () => {
    expect(runLabel("outline", null)).toBe("outline");
    expect(runLabel("outline", "   ")).toBe("outline");
    expect(runLabel("outline", undefined)).toBe("outline");
  });
  it("joins title and scope with ': '", () => {
    expect(runLabel("outline", " section two ")).toBe("outline: section two");
  });
  it("splits back on the FIRST ': ' so a scope may carry its own", () => {
    expect(splitLabel("outline: section two: the hook")).toEqual({
      title: "outline",
      scope: "section two: the hook",
    });
    expect(splitLabel("outline")).toEqual({ title: "outline", scope: null });
    // a colon without the space is part of the title, not a separator
    expect(splitLabel("10:30 standup")).toEqual({
      title: "10:30 standup",
      scope: null,
    });
  });
  it("round-trips", () => {
    const label = runLabel("mix", "vocals: comp the chorus");
    expect(splitLabel(label)).toEqual({
      title: "mix",
      scope: "vocals: comp the chorus",
    });
  });
  it("caps match the server", () => {
    expect(SCOPE_CAP).toBe(140);
  });
});

describe("the skip memory", () => {
  it("is per task per day, and forgets tomorrow", () => {
    const s = new MemoryStorage();
    expect(scopeSkipped(s, 7, "2026-09-04")).toBe(false);
    rememberScopeSkip(s, 7, "2026-09-04");
    expect(scopeSkipped(s, 7, "2026-09-04")).toBe(true);
    expect(scopeSkipped(s, 8, "2026-09-04")).toBe(false);
    expect(scopeSkipped(s, 7, "2026-09-05")).toBe(false);
  });
  it("survives a storage that throws", () => {
    const broken = {
      getItem() {
        throw new Error("nope");
      },
      setItem() {
        throw new Error("nope");
      },
    } as unknown as Storage;
    expect(scopeSkipped(broken, 1, "2026-09-04")).toBe(false);
    expect(() => rememberScopeSkip(broken, 1, "2026-09-04")).not.toThrow();
  });
});

describe("tomorrowOf", () => {
  it("steps one calendar day without a timezone in sight", () => {
    expect(tomorrowOf("2026-09-04")).toBe("2026-09-05");
    expect(tomorrowOf("2026-09-30")).toBe("2026-10-01");
    expect(tomorrowOf("2026-12-31")).toBe("2027-01-01");
    expect(tomorrowOf("2028-02-28")).toBe("2028-02-29");
  });
});

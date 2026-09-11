import { describe, expect, it } from "vitest";
import {
  applyTheme,
  readTheme,
  resolveTheme,
  saveTheme,
  systemTheme,
  THEME_KEY,
} from "./theme";

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

describe("theme choice", () => {
  it("reads only a known theme, and nothing when unset", () => {
    const storage = memoryStorage();
    expect(readTheme(storage)).toBeNull();
    storage.setItem(THEME_KEY, "sepia");
    expect(readTheme(storage)).toBeNull();
    saveTheme(storage, "light");
    expect(readTheme(storage)).toBe("light");
  });

  it("survives a storage that throws", () => {
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    } as unknown as Storage;
    expect(readTheme(broken)).toBeNull();
    expect(() => saveTheme(broken, "dark")).not.toThrow();
    expect(readTheme(null)).toBeNull();
  });

  it("the stored choice beats the system, and the system is the fallback", () => {
    expect(resolveTheme("light", "dark")).toBe("light");
    expect(resolveTheme(null, "dark")).toBe("dark");
    expect(systemTheme(() => ({ matches: true }))).toBe("dark");
    expect(systemTheme(() => ({ matches: false }))).toBe("light");
    expect(systemTheme(undefined)).toBe("light");
  });

  it("applies as a data attribute the stylesheet can key on", () => {
    const root = { dataset: {} as DOMStringMap, style: {} as CSSStyleDeclaration };
    applyTheme(root as unknown as HTMLElement, "light");
    expect(root.dataset.theme).toBe("light");
    expect(root.style.colorScheme).toBe("light");
  });
});

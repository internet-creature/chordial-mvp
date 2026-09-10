// the colour theme: one stored choice shared by both windows. the choice
// lives in localStorage so the companion window sees it through the
// storage event; with no choice stored, the system preference decides.

export type Theme = "dark" | "light";

export const THEME_KEY = "chordial.theme";
export const THEME_EVENT = "chordial:theme";

export function isTheme(value: unknown): value is Theme {
  return value === "dark" || value === "light";
}

/** the stored choice, if any */
export function readTheme(storage: Storage | null): Theme | null {
  try {
    const raw = storage?.getItem(THEME_KEY);
    return isTheme(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function saveTheme(storage: Storage | null, theme: Theme): void {
  try {
    storage?.setItem(THEME_KEY, theme);
  } catch {
    // the choice still applies for this session
  }
}

/** what the system prefers; light when the query is unavailable */
export function systemTheme(
  matchMedia: ((query: string) => { matches: boolean }) | undefined,
): Theme {
  try {
    return matchMedia?.("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  } catch {
    return "light";
  }
}

/** the stored choice wins; otherwise the system's */
export function resolveTheme(stored: Theme | null, system: Theme): Theme {
  return stored ?? system;
}

export function applyTheme(root: HTMLElement, theme: Theme): void {
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}

/** the theme this window should show right now */
export function currentTheme(win: Window): Theme {
  return resolveTheme(
    readTheme(safeStorage(win)),
    systemTheme(win.matchMedia?.bind(win)),
  );
}

/** choose a theme: store it, apply it here, and tell this window's
 * listeners. other windows hear the storage event. */
export function setTheme(win: Window, theme: Theme): void {
  saveTheme(safeStorage(win), theme);
  applyTheme(win.document.documentElement, theme);
  win.dispatchEvent(new Event(THEME_EVENT));
}

/** apply the current theme and keep it applied: a change in another
 * window, or a system change while no choice is stored, re-applies. */
export function watchTheme(
  win: Window,
  onChange?: (theme: Theme) => void,
): () => void {
  const apply = () => {
    const theme = currentTheme(win);
    applyTheme(win.document.documentElement, theme);
    onChange?.(theme);
  };
  apply();
  const storage = (event: StorageEvent) => {
    if (event.key === THEME_KEY || event.key === null) apply();
  };
  const media = win.matchMedia?.("(prefers-color-scheme: dark)");
  win.addEventListener("storage", storage);
  win.addEventListener(THEME_EVENT, apply);
  media?.addEventListener?.("change", apply);
  return () => {
    win.removeEventListener("storage", storage);
    win.removeEventListener(THEME_EVENT, apply);
    media?.removeEventListener?.("change", apply);
  };
}

function safeStorage(win: Window): Storage | null {
  try {
    return win.localStorage;
  } catch {
    return null;
  }
}

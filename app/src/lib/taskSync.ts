// Invalidation only: no task data or credentials cross window boundaries.
export const TASKS_REV_KEY = "chordial.tasks.revision";
const TASKS_EVENT = "chordial:tasks-changed";

export function announceTasksChanged(): void {
  window.dispatchEvent(new Event(TASKS_EVENT));
  try {
    window.localStorage.setItem(TASKS_REV_KEY, crypto.randomUUID());
  } catch {
    // Focus/visibility refresh and polling still work without storage.
  }
}

/** The main window owns the server socket. Its deliveries invalidate both
 * windows; polling also catches phone edits when that window is closed. */
export function watchTaskChanges(refresh: () => void): () => void {
  const visibleRefresh = () => {
    if (document.visibilityState !== "hidden") refresh();
  };
  const storage = (event: StorageEvent) => {
    if (event.key === TASKS_REV_KEY || event.key === null) refresh();
  };
  window.addEventListener(TASKS_EVENT, refresh);
  window.addEventListener("storage", storage);
  window.addEventListener("focus", visibleRefresh);
  window.addEventListener("online", visibleRefresh);
  document.addEventListener("visibilitychange", visibleRefresh);
  const timer = window.setInterval(visibleRefresh, 30_000);
  return () => {
    window.removeEventListener(TASKS_EVENT, refresh);
    window.removeEventListener("storage", storage);
    window.removeEventListener("focus", visibleRefresh);
    window.removeEventListener("online", visibleRefresh);
    document.removeEventListener("visibilitychange", visibleRefresh);
    window.clearInterval(timer);
  };
}

/** Serialize reads and repeat once if invalidated during a read. A slow
 * pre-mutation response must never be the last snapshot we display. */
export function refreshQueue<T>(
  load: () => Promise<T>,
  apply: (value: T) => void,
  fail: (error: unknown) => void,
) {
  let running = false;
  let pending = false;
  let disposed = false;
  const refresh = async (): Promise<void> => {
    if (disposed) return;
    if (running) {
      pending = true;
      return;
    }
    running = true;
    do {
      pending = false;
      try {
        const value = await load();
        if (!disposed) apply(value);
      } catch (error) {
        if (!disposed) fail(error);
      }
    } while (pending && !disposed);
    running = false;
  };
  return {
    refresh,
    dispose: () => {
      disposed = true;
    },
  };
}

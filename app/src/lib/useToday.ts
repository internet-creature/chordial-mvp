import { useEffect, useState } from "react";
import { fetchToday, isAuthError } from "../api/client";
import type { TodayPayload } from "../api/types";
import {
  announceTasksChanged,
  refreshQueue,
  watchTaskChanges,
} from "./taskSync";

export function useToday(token: string | null, onAuthLost?: () => void) {
  const [today, setToday] = useState<TodayPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  useEffect(() => {
    setToday(null);
    setError(null);
    setUpdatedAt(null);
    if (!token) return;
    const controller = new AbortController();
    const queue = refreshQueue(
      () => fetchToday(token, controller.signal),
      (value) => {
        setToday(value);
        setError(null);
        setUpdatedAt(new Date());
      },
      (err) => {
        if (isAuthError(err)) {
          setToday(null);
          setUpdatedAt(null);
          setError("device link expired — reopen chordial to relink");
          onAuthLost?.();
        } else {
          setError("can’t reach your tasks — retrying");
        }
      },
    );
    const stop = watchTaskChanges(() => {
      void queue.refresh();
    });
    void queue.refresh();
    return () => {
      stop();
      queue.dispose();
      controller.abort();
    };
  }, [token, onAuthLost]);

  return { today, error, updatedAt, refreshToday: announceTasksChanged };
}

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchCycle, isAuthError } from "../api/client";
import { startFocus } from "../api/sidecar";
import type { CommitmentRow, CyclePayload } from "../api/types";
import {
  capacityFill,
  capacityLine,
  commitmentFill,
  visibleCommitments,
} from "../lib/cycle";

interface Props {
  token: string;
  pomMinutes: number;
  onAuthLost: () => void;
}

/** the shared cycle state on the home screen: theme, capacity, every
 * commitment with its real progress (banked minutes flow in from the
 * companion through the sync contract), and the one-tap door - a next
 * action starts a block on the companion's clock. */
export default function CyclePanel({ token, pomMinutes, onAuthLost }: Props) {
  const [view, setView] = useState<CyclePayload | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<number | null>(null);
  // the poll must DIE with the session: an interval that keeps 401-ing
  // over the link screen is noise and a repeated onAuthLost
  const timerRef = useRef<number | null>(null);
  // double-tap guard that doesn't wait for a react re-render
  const startingRef = useRef(false);

  const refresh = useCallback(() => {
    fetchCycle(token)
      .then(setView)
      .catch((e) => {
        if (isAuthError(e)) {
          if (timerRef.current !== null) {
            window.clearInterval(timerRef.current);
            timerRef.current = null;
          }
          onAuthLost();
        }
        // otherwise stay quietly absent - the panel is a window, not a wall
      });
  }, [token, onAuthLost]);

  useEffect(() => {
    refresh();
    // the projection moves when blocks land; a slow poll keeps the panel
    // honest while the app sits open (the deer syncs every ~15s)
    timerRef.current = window.setInterval(refresh, 20_000);
    return () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [refresh]);

  async function startBlock(c: CommitmentRow) {
    if (startingRef.current) return;
    startingRef.current = true;
    setNote(null);
    setStartingId(c.id);
    try {
      await startFocus(
        c.task_id,
        c.next_action ?? c.task_title ?? c.title,
        pomMinutes,
      );
      setNote("timer started");
    } catch (e) {
      // the sidecar answering with a refusal ("that task's clock is
      // already running") is a different truth than the sidecar being
      // gone - a network failure is a TypeError, an answered error isn't
      setNote(
        e instanceof TypeError
          ? "timer unavailable — open the companion and retry."
          : e instanceof Error && e.message
            ? e.message
            : "that didn’t take — try again?",
      );
    } finally {
      startingRef.current = false;
      setStartingId(null);
      window.setTimeout(() => setNote(null), 5000);
    }
  }

  if (!view || !view.cycle) return null;
  const { cycle, totals } = view;
  const commitments = visibleCommitments(view.commitments ?? []);
  const bar = totals ? capacityFill(totals) : null;

  return (
    <section className="cycle-panel">
      <header className="cycle-head">
        <div>
          <h3>cycle</h3>
          {cycle.theme && <p className="cycle-theme">{cycle.theme}</p>}
        </div>
        {view.frozen && (
          <span className="cycle-frozen" title="the baseline is frozen">
            baseline set
          </span>
        )}
      </header>

      {totals && bar && (
        <div className="cycle-capacity">
          <div className="cycle-bar" aria-hidden="true">
            <div
              className="cycle-bar-planned"
              style={{ width: `${bar.planned * 100}%` }}
            />
            <div
              className="cycle-bar-done"
              style={{ width: `${bar.done * 100}%` }}
            />
          </div>
          <p className="cycle-capacity-line">{capacityLine(totals)}</p>
        </div>
      )}

      <ul className="cycle-commitments">
        {commitments.map((c) => {
          const done = c.status === "completed";
          return (
            <li key={c.uuid} className={`commitment${done ? " done" : ""}`}>
              <div className="commitment-row">
                <span
                  className={`task-dot ${c.priority ?? "none"}`}
                  aria-hidden="true"
                />
                <span className="commitment-title">
                  {done ? "✓ " : ""}
                  {c.title}
                </span>
                <span className="commitment-blocks">
                  {c.blocks_done}
                  {c.blocks_planned != null && ` / ${c.blocks_planned}`}
                </span>
              </div>
              <div className="commitment-fill" aria-hidden="true">
                <div
                  className="commitment-fill-bar"
                  style={{ width: `${commitmentFill(c) * 100}%` }}
                />
              </div>
              {!done && c.next_action && (
                <div className="next-action">
                  <span className="next-action-label">next</span>
                  <span className="next-action-text">{c.next_action}</span>
                  <button
                    className="next-action-start"
                    onClick={() => startBlock(c)}
                    disabled={startingId !== null}
                  >
                    {startingId === c.id ? "starting…" : "start a block ▸"}
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {note && <p className="cycle-note">{note}</p>}
    </section>
  );
}

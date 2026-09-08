import { useEffect, useState } from "react";
import {
  fetchArc,
  fetchArchive,
  fetchCycleDoors,
  fetchToday,
  isAuthError,
  openCyclePlanning,
  openCycleRetro,
} from "../api/client";
import type {
  ArchivedRoom,
  ArcState,
  CycleDoorsPayload,
  TaskRow,
  TodayPayload,
} from "../api/types";
import type { CycleRoomHandle } from "./Room";
import CyclePanel from "./CyclePanel";

interface Props {
  token: string;
  /** council lines that arrived while nobody was in a room view (7a):
   * the door carries the nudge instead of a notification interrupting */
  unread: number;
  onEnterRoom: () => void;
  onEnterCycleRoom: (handle: CycleRoomHandle) => void;
  onOpenArchive: (room: ArchivedRoom) => void;
  onAuthLost: () => void;
}

function greeting(now: Date): string {
  const h = now.getHours();
  if (h < 5) return "up late";
  if (h < 12) return "good morning";
  if (h < 18) return "good afternoon";
  return "good evening";
}

/** minutes as a human beat — mirrors the server's render ("2 hours") */
function formatBeat(minutes: number): string {
  if (minutes > 0 && minutes % 60 === 0) {
    const h = minutes / 60;
    return h === 1 ? "1 hour" : `${h} hours`;
  }
  return `${minutes} minutes`;
}

function TaskList({
  label,
  tasks,
  muted = false,
}: {
  label: string;
  tasks: TaskRow[];
  /** the set-aside group: parked for today, greyed (§2) */
  muted?: boolean;
}) {
  if (tasks.length === 0) return null;
  return (
    <section className={`task-group${muted ? " muted" : ""}`}>
      <h3>{label}</h3>
      <ul>
        {tasks.map((t) => (
          <li key={t.id} className="task-row">
            <span
              className={`task-dot ${t.priority ?? "none"}`}
              aria-hidden="true"
            />
            <span className="task-title">{t.title}</span>
            {t.next_action && (
              <span className="task-scope">↳ {t.next_action}</span>
            )}
            {t.plan_title && <span className="task-plan">{t.plan_title}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** the landing: a greeting, the shape of today, the shared cycle, the door
 * to the room, and the journal of remembered days. */
export default function Home({
  token,
  unread,
  onEnterRoom,
  onEnterCycleRoom,
  onOpenArchive,
  onAuthLost,
}: Props) {
  const [today, setToday] = useState<TodayPayload | null>(null);
  const [pastDays, setPastDays] = useState<ArchivedRoom[]>([]);
  const [doors, setDoors] = useState<CycleDoorsPayload | null>(null);
  const [arc, setArc] = useState<ArcState | null>(null);
  const [opening, setOpening] = useState<"retro" | "planning" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchToday(token)
      .then((payload) => {
        if (!cancelled) setToday(payload);
      })
      .catch((e) => {
        if (cancelled) return;
        if (isAuthError(e)) onAuthLost();
        else setError(e instanceof Error ? e.message : "couldn’t load today");
      });
    fetchArchive(token)
      .then((body) => {
        if (cancelled) return;
        // the journal shows what's settled: closed days only
        setPastDays(body.rooms.filter((r) => r.status === "closed"));
      })
      .catch((e) => {
        if (!cancelled && isAuthError(e)) onAuthLost();
        // a missing journal is not an error worth a banner
      });
    fetchCycleDoors(token)
      .then((body) => {
        if (!cancelled) setDoors(body);
      })
      .catch((e) => {
        if (!cancelled && isAuthError(e)) onAuthLost();
        // no doors is a fine state, not a banner
      });
    fetchArc(token)
      .then((body) => {
        if (!cancelled) setArc(body.arc);
      })
      .catch((e) => {
        if (!cancelled && isAuthError(e)) onAuthLost();
        // the arc line is a grace note, never a banner
      });
    return () => {
      cancelled = true;
    };
  }, [token, onAuthLost]);

  /** open (get-or-create) a cycle room and walk in. the POST is
   * idempotent server-side, so a double-tap is harmless; `opening`
   * just keeps the button honest while the first tap is in flight. */
  async function enterCycleRoom(kind: "retro" | "planning") {
    if (opening) return;
    setOpening(kind);
    try {
      const result =
        kind === "retro"
          ? await openCycleRetro(token)
          : await openCyclePlanning(token);
      onEnterCycleRoom({
        id: result.room.id,
        type: result.room.type,
        label: result.cycle.title,
        chatAvailable: doors?.chat_available ?? true,
      });
    } catch (e) {
      if (isAuthError(e)) onAuthLost();
      else
        setError(
          e instanceof Error ? e.message : "couldn’t open the cycle room",
        );
    } finally {
      setOpening(null);
    }
  }

  const name = today?.user.name;
  const buckets = today?.buckets;
  const empty =
    buckets &&
    buckets.today.length === 0 &&
    buckets.overdue.length === 0 &&
    buckets.in_progress.length === 0 &&
    buckets.set_aside.length === 0;

  const dateLine = today
    ? new Date(`${today.today}T12:00:00`).toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      })
    : "";

  return (
    <div className="home">
      <header className="home-head">
        <h1>
          {greeting(new Date())}
          {name ? `, ${name}` : ""}
        </h1>
        <p className="home-date">{dateLine}</p>
      </header>

      {error && <p className="soft-error">{error}</p>}

      {buckets && (
        <div className="home-tasks">
          <TaskList label="in motion" tasks={buckets.in_progress} />
          <TaskList label="today" tasks={buckets.today} />
          <TaskList label="carried over" tasks={buckets.overdue} />
          <TaskList label="set aside" tasks={buckets.set_aside} muted />
          {empty && (
            <p className="home-empty">
              nothing on the list — a quiet day is allowed.
            </p>
          )}
        </div>
      )}

      <CyclePanel
        token={token}
        pomMinutes={today?.pom_minutes ?? 25}
        onAuthLost={onAuthLost}
      />

      {arc && arc.multiplier > 1 && (
        <p className="arc-line">
          the house is {arc.posture === "keeping watch" ? "" : "in "}
          {arc.posture} — {arc.streak} steady{" "}
          {arc.streak === 1 ? "cycle has" : "cycles have"} stretched
          check-ins to about every {formatBeat(arc.checkin_minutes)} 🌿
        </p>
      )}

      {doors?.doors && (
        <section className="cycle-doors">
          <h3>
            cycle “{doors.doors.cycle.title}” closed
            {doors.doors.scored ? " — the card is filed" : ""}
          </h3>
          <div className="cycle-door-row">
            <button
              className="cycle-door"
              onClick={() => enterCycleRoom("retro")}
              disabled={opening !== null}
            >
              {doors.doors.retro
                ? "return to the retro →"
                : "sit down for the retro →"}
            </button>
            <button
              className="cycle-door"
              onClick={() => enterCycleRoom("planning")}
              disabled={opening !== null}
            >
              {doors.doors.planning
                ? "back to planning →"
                : "plan the next cycle →"}
            </button>
          </div>
        </section>
      )}

      <button className="enter-room" onClick={onEnterRoom}>
        step into today’s room →
        {unread > 0 && (
          <span
            className="room-nudge"
            title={`${unread} new ${unread === 1 ? "line" : "lines"} waiting`}
          >
            {unread}
          </span>
        )}
      </button>

      {pastDays.length > 0 && (
        <section className="past-days">
          <h3>remembered days</h3>
          <ul>
            {pastDays.map((r) => (
              <li key={r.room_uuid}>
                <button
                  className="past-day"
                  onClick={() => onOpenArchive(r)}
                >
                  <span className="past-day-date">
                    {r.date
                      ? new Date(`${r.date}T12:00:00`).toLocaleDateString(
                          undefined,
                          { weekday: "short", month: "short", day: "numeric" },
                        )
                      : r.room_type === "cycle_retro"
                        ? `retrospective (${r.subject_id ?? "cycle"})`
                        : r.room_type === "cycle_planning"
                          ? `planning (${r.subject_id ?? "cycle"})`
                          : "before the rooms"}
                  </span>
                  {r.summary_line && (
                    <span className="past-day-hint">{r.summary_line}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

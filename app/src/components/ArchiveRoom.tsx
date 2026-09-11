import { useEffect, useState } from "react";
import { fetchArchivedMessages, isAuthError } from "../api/client";
import type { ArchivedRoom, CouncilMember, HistoryRow } from "../api/types";
import { memberHue } from "../lib/council";
import InlineContent from "./InlineContent";

interface Props {
  token: string;
  council: CouncilMember[];
  room: ArchivedRoom;
  onEnterToday: () => void;
  onBack: () => void;
  onAuthLost: () => void;
}

/** a remembered day: the archived room, read-only. reopening is for
 * remembering - the gentle door at the bottom leads back to today. */
export default function ArchiveRoom({
  token,
  council,
  room,
  onEnterToday,
  onBack,
  onAuthLost,
}: Props) {
  const [messages, setMessages] = useState<HistoryRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchArchivedMessages(token, room.room_uuid)
      .then((body) => {
        if (!cancelled) setMessages(body.messages);
      })
      .catch((e) => {
        if (cancelled) return;
        if (isAuthError(e)) onAuthLost();
        else setError("couldn’t open that day");
      });
    return () => {
      cancelled = true;
    };
  }, [token, room.room_uuid, onAuthLost]);

  const byId = new Map(council.map((m) => [m.id, m]));
  const dateLine = room.date
    ? new Date(`${room.date}T12:00:00`).toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      })
    : "earlier conversations";

  return (
    <div className="room archive">
      <header className="room-head">
        <div>
          <h2>{dateLine}</h2>
        </div>
        <button className="archive-back" onClick={onBack}>
          ← back
        </button>
      </header>

      {room.summary && (
        // collapsed by default: the digest's tail quotes conversation, and
        // an opened journal on a shared screen shouldn't volunteer it -
        // one click opens it, the transcript below is scrollable anyway
        <details className="archive-summary">
          <summary className="archive-summary-label">
            summary
          </summary>
          <pre>{room.summary}</pre>
        </details>
      )}

      <div className="room-log">
        {error && <p className="soft-error">{error}</p>}
        {messages && messages.length === 0 && (
          <p className="room-empty">no messages.</p>
        )}
        {(messages ?? []).map((m) => {
          if (m.author_type === "user") {
            return (
              <div key={m.id} className="line mine">
                <div className="bubble">{m.content}</div>
              </div>
            );
          }
          const member = byId.get(m.author);
          return (
            <div key={m.id} className="line theirs">
              <div className="line-head">
                <span className="line-avatar" aria-hidden="true">
                  {member?.emoji ?? "✨"}
                </span>
                <span
                  className="line-name"
                  style={{ color: memberHue(m.author) }}
                >
                  {member?.name ?? m.author}
                </span>
              </div>
              <div className="bubble">
                <InlineContent content={m.content} />
              </div>
            </div>
          );
        })}
      </div>

      <button className="enter-room" onClick={onEnterToday}>
        continue in today’s room →
      </button>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchArchivedMessages,
  fetchRoomCurrent,
  fetchRoomMessages,
  isAuthError,
  sendRoomMessage,
} from "../api/client";
import { type SocketBus, type SocketStatus } from "../api/ws";
import type { CouncilMember, RoomInfo } from "../api/types";
import {
  admitLive,
  fromHistory,
  pendingUserLine,
  reconcileExtras,
  type ChatMessage,
  type RoomFilter,
} from "../lib/messages";
import { memberHue } from "../lib/council";
import { pickStirring } from "../lib/stirrings";
import InlineContent from "./InlineContent";

/** a cycle room bound by uuid (phase 6b). when present, the view leaves
 * the current-daily contract: history and sends go by room uuid, and the
 * websocket filter turns strict - only lines naming this room render. */
export interface CycleRoomHandle {
  id: string;
  type: "cycle_retro" | "cycle_planning";
  /** header line, e.g. "retrospective — rewillow v1" */
  label: string;
  chatAvailable: boolean;
}

interface Props {
  token: string;
  council: CouncilMember[];
  /** the shell-owned socket's fan-out (7a): the view subscribes while
   * mounted; the socket itself outlives navigation */
  bus: SocketBus;
  socketStatus: SocketStatus;
  onTurnComplete: () => void;
  onAuthLost: () => void;
  cycleRoom?: CycleRoomHandle;
  onBack?: () => void;
}

const STATUS_DOT: Record<SocketStatus, string> = {
  open: "live",
  connecting: "waking",
  closed: "away",
  revoked: "away",
};

const STIRRING_ROTATE_MS = 2400;

/** the waiting line: a small creature doing small things, a new one every
 * couple of seconds so the wait feels inhabited rather than stuck */
function Stirring() {
  const [phrase, setPhrase] = useState(() => pickStirring());
  useEffect(() => {
    const timer = setInterval(
      () => setPhrase((current) => pickStirring(current)),
      STIRRING_ROTATE_MS,
    );
    return () => clearInterval(timer);
  }, []);
  return (
    <p className="stirring">
      <span className="stir-dot" />
      <span className="stir-dot" />
      <span className="stir-dot" />
      <span key={phrase} className="stir-word">
        {phrase}…
      </span>
    </p>
  );
}

/** today's room: the shared space where the council answers. history is the
 * persisted log; live lines arrive over the websocket and the send response
 * (deduped by payload id), and the composer's own line shows optimistically. */
export default function Room({
  token,
  council,
  bus,
  socketStatus,
  onTurnComplete,
  onAuthLost,
  cycleRoom,
  onBack,
}: Props) {
  const [room, setRoom] = useState<RoomInfo | null>(null);
  const [chatAvailable, setChatAvailable] = useState(
    cycleRoom ? cycleRoom.chatAvailable : true,
  );
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [extras, setExtras] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const seen = useRef<Set<string>>(new Set());
  const logRef = useRef<HTMLDivElement | null>(null);
  const pinnedToBottom = useRef(true);
  // the websocket filter reads this ref so the socket never resubscribes
  // when the room resolves: cycle rooms know their uuid up front, the
  // daily view learns it from the first fetch
  const roomIdRef = useRef<string | null>(cycleRoom?.id ?? null);

  const refresh = useCallback(async () => {
    try {
      if (cycleRoom) {
        const log = await fetchArchivedMessages(token, cycleRoom.id);
        const rows = log.messages.map(fromHistory);
        setHistory(rows);
        setExtras((prev) => reconcileExtras(prev, rows));
        return;
      }
      const [current, log] = await Promise.all([
        fetchRoomCurrent(token),
        fetchRoomMessages(token),
      ]);
      setRoom(current.room);
      roomIdRef.current = current.room.id;
      setChatAvailable(current.chat_available);
      const rows = log.messages.map(fromHistory);
      setHistory(rows);
      setExtras((prev) => reconcileExtras(prev, rows));
    } catch (e) {
      if (isAuthError(e)) onAuthLost();
    }
  }, [token, onAuthLost, cycleRoom]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    // the socket is the shell's (it outlives this view); subscribe for the
    // lines and reconnects this room cares about while mounted.
    // dedupe OUTSIDE the state updater: updaters must stay pure (strict
    // mode double-invokes them), the seen-set mutation must run once.
    // the filter keeps rooms apart on the shared per-user socket: a
    // retro line never renders in the daily view and vice versa
    const offPayload = bus.onPayload((payload) => {
      const filter: RoomFilter = {
        room: roomIdRef.current,
        strict: Boolean(cycleRoom),
      };
      const line = admitLive(seen.current, payload, filter);
      if (line) setExtras((prev) => [...prev, line]);
    });
    const offReconnect = bus.onReconnect(refresh);
    return () => {
      offPayload();
      offReconnect();
    };
  }, [bus, refresh, cycleRoom]);

  const messages = [...history, ...extras];

  // stay pinned to the newest line unless the reader scrolled up on purpose
  useEffect(() => {
    const el = logRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, sending]);

  function onLogScroll() {
    const el = logRef.current;
    if (!el) return;
    pinnedToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }

  /** run (or re-run) one send under its receipt key. a failure keeps the
   * line - marked failed, id preserved - because the failure may be
   * ambiguous: the server can finish a turn whose response never reached
   * us, and only a retry under the SAME client_message_id replays the
   * stored receipt instead of paying for (and mutating with) a second turn. */
  async function runSend(mine: ChatMessage) {
    setSendError(null);
    setSending(true);
    try {
      const result = await sendRoomMessage(
        token,
        mine.content,
        mine.clientMessageId!,
        cycleRoom?.id,
      );
      // the POST drains the per-user queue, which can carry another room's
      // concurrent lines - filter like the socket does. ephemeral copy
      // (refusals/errors) bypasses the filter: it exists only in this
      // response, for this send, and never carries a room
      const filter: RoomFilter = {
        room: roomIdRef.current,
        strict: Boolean(cycleRoom),
      };
      const fresh = result.messages
        .map((payload) =>
          payload.ephemeral
            ? admitLive(seen.current, payload)
            : admitLive(seen.current, payload, filter),
        )
        .filter((line): line is ChatMessage => line !== null);
      setExtras((prev) => [
        ...prev.map((m) =>
          m.key === mine.key ? { ...m, pending: false, failed: false } : m,
        ),
        ...fresh,
      ]);
      onTurnComplete();
      // the server routed this turn to the CURRENT local-day room; refetch
      // so a lazy rollover (app open across midnight) swaps the header and
      // history to the new day instead of appending under yesterday
      await refresh();
    } catch (e) {
      if (isAuthError(e)) {
        onAuthLost();
        return;
      }
      setExtras((prev) =>
        prev.map((m) =>
          m.key === mine.key ? { ...m, pending: false, failed: true } : m,
        ),
      );
      setSendError(e instanceof Error ? e.message : "that didn’t go through");
    } finally {
      setSending(false);
    }
  }

  function send() {
    const text = draft.trim();
    if (!text || sending || !chatAvailable) return;
    const mine = pendingUserLine(crypto.randomUUID(), text);
    setDraft("");
    setExtras((prev) => [...prev, mine]);
    pinnedToBottom.current = true;
    void runSend(mine);
  }

  function retry(mine: ChatMessage) {
    if (sending || !mine.clientMessageId) return;
    setExtras((prev) =>
      prev.map((m) =>
        m.key === mine.key ? { ...m, pending: true, failed: false } : m,
      ),
    );
    void runSend(mine);
  }

  function onComposerKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const byId = new Map(council.map((m) => [m.id, m]));
  const dateLine = room?.date
    ? new Date(`${room.date}T12:00:00`).toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      })
    : "";
  const title = cycleRoom
    ? cycleRoom.type === "cycle_retro"
      ? "the retrospective"
      : "planning the next cycle"
    : "today’s room";
  const subtitle = cycleRoom ? cycleRoom.label : dateLine;
  const emptyCopy = cycleRoom
    ? cycleRoom.type === "cycle_retro"
      ? "review this cycle."
      : "plan your next cycle."
    : "start a conversation.";

  return (
    <div className="room">
      <header className="room-head">
        <div>
          {onBack && (
            <button className="room-back" onClick={onBack}>
              ← back
            </button>
          )}
          <h2>{title}</h2>
          <p className="room-date">{subtitle}</p>
        </div>
        <span className="room-connection" role="status">
          <span className={`ws-dot ${STATUS_DOT[socketStatus]}`} aria-hidden="true" />
          {socketStatus === "open" ? "connected" : socketStatus === "connecting" ? "connecting…" : "offline"}
        </span>
      </header>

      <div className="room-log" ref={logRef} onScroll={onLogScroll}>
        {messages.length === 0 && !sending && (
          <p className="room-empty">{emptyCopy}</p>
        )}
        {messages.map((m, i) => {
          const prev = messages[i - 1];
          const grouped =
            prev &&
            prev.author === m.author &&
            prev.authorType === m.authorType &&
            !m.ephemeral &&
            !prev.ephemeral;
          if (m.authorType === "user") {
            return (
              <div
                key={m.key}
                className={`line mine${m.pending ? " pending" : ""}`}
              >
                <div className="bubble">{m.content}</div>
                {m.platform === "telegram" && (
                  <span className="line-via" title="sent from the phone">
                    📱 from telegram
                  </span>
                )}
                {m.failed && (
                  <button
                    className="line-retry"
                    onClick={() => retry(m)}
                    disabled={sending}
                  >
                    didn’t go through — send again
                  </button>
                )}
              </div>
            );
          }
          const member = byId.get(m.author);
          const hue = memberHue(m.author);
          return (
            <div
              key={m.key}
              className={`line theirs${m.ephemeral ? " ephemeral" : ""}`}
            >
              {!grouped && (
                <div className="line-head">
                  <span className="line-avatar" aria-hidden="true">
                    {member?.emoji ?? "✨"}
                  </span>
                  <span className="line-name" style={{ color: hue }}>
                    {member?.name ?? m.author}
                  </span>
                </div>
              )}
              <div className="bubble">
                <InlineContent content={m.content} />
              </div>
            </div>
          );
        })}
        {sending && <Stirring />}
      </div>

      {sendError && <p className="soft-error">{sendError}</p>}

      {chatAvailable ? (
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.currentTarget.value)}
            onKeyDown={onComposerKey}
            placeholder="message…"
            rows={2}
            disabled={sending}
          />
          <button type="submit" disabled={sending || !draft.trim()}>
            send
          </button>
        </form>
      ) : (
        <p className="room-quiet">
          chat is unavailable on this connection.
        </p>
      )}
    </div>
  );
}

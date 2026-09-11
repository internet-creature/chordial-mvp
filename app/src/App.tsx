import { useCallback, useEffect, useRef, useState } from "react";
import LinkScreen from "./components/LinkScreen";
import PresenceRail from "./components/PresenceRail";
import Home from "./components/Home";
import Room, { type CycleRoomHandle } from "./components/Room";
import ArchiveRoom from "./components/ArchiveRoom";
import StuckPage from "./components/StuckPage";
import { fetchCouncil, fetchRoomCurrent, isAuthError } from "./api/client";
import { fetchSidecarState } from "./api/sidecar";
import { RoomSocket, SocketBus, type SocketStatus } from "./api/ws";
import type { ArchivedRoom, CouncilMember } from "./api/types";
import { classifyForNudge } from "./lib/unread";
import { clearSession, loadSession, storeSession } from "./lib/session";
import { announceTasksChanged } from "./lib/taskSync";
import { inTauri } from "./lib/tauriWindow";
import {
  makeRequest,
  readStuckRequest,
  STUCK_REQUEST_KEY,
  type StuckRequest,
} from "./lib/stuck";
import type { StuckSurface } from "./api/types";
import { listen } from "@tauri-apps/api/event";

type View = "home" | "room" | "archive" | "cycle" | "stuck";

// heartbeat every 30s; the server counts ~3 missed beats as away
const PRESENCE_BEAT_MS = 30_000;

/** the shell: link screen until a device token exists, then the rail plus
 * whichever space you're in. losing auth anywhere (revoked device) drops
 * cleanly back to the front porch.
 *
 * the shell owns the ONE server socket (7a): it stays up across navigation
 * so proactive lines land (and confirm) even from the home screen, presence
 * heartbeats ride it, and the room views subscribe through a bus while
 * mounted. lines that arrive while nobody is watching the room become the
 * door's unread nudge. */
export default function App() {
  // the token loads async now (7b: the keychain answers a beat after
  // launch); sessionLoaded gates the render so the link screen never
  // flashes at someone who is already linked
  const [token, setToken] = useState<string | null>(null);
  const [sessionLoaded, setSessionLoaded] = useState(false);
  const [view, setView] = useState<View>("home");
  const [council, setCouncil] = useState<CouncilMember[]>([]);
  const [archiveRoom, setArchiveRoom] = useState<ArchivedRoom | null>(null);
  const [cycleRoom, setCycleRoom] = useState<CycleRoomHandle | null>(null);
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("connecting");
  const [unread, setUnread] = useState(0);
  // the press behind the stuck page: from Home directly, from the companion
  // or the tray through localStorage (docs/STUCK_MODE_DESIGN.md §1)
  const [stuckRequest, setStuckRequest] = useState<StuckRequest | null>(null);

  const busRef = useRef<SocketBus | null>(null);
  if (!busRef.current) busRef.current = new SocketBus();
  const bus = busRef.current;

  // the socket callback checks the CURRENT view without resubscribing on
  // every navigation
  const viewRef = useRef(view);
  viewRef.current = view;

  // today's daily room, for room-aware unread counting: the badge counts
  // ONLY daily lines, so a cycle room's delayed replies never inflate the
  // daily door and daily lines still nudge while a cycle room is open.
  // refreshed lazily when an unrecognized room id arrives (midnight
  // rollover mints a new daily room).
  const dailyRoomRef = useRef<string | null>(null);
  const resolvingDaily = useRef<Promise<void> | null>(null);

  const resolveDailyRoom = useCallback(
    (authToken: string): Promise<void> => {
      if (!resolvingDaily.current) {
        resolvingDaily.current = fetchRoomCurrent(authToken)
          .then((current) => {
            dailyRoomRef.current = current.room.id;
          })
          .catch(() => undefined) // the badge is a grace note, never an error
          .finally(() => {
            resolvingDaily.current = null;
          });
      }
      return resolvingDaily.current;
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    void loadSession().then((held) => {
      if (!cancelled) {
        setToken(held);
        setSessionLoaded(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const onAuthLost = useCallback(() => {
    void clearSession();
    setToken(null);
    setCouncil([]);
    setView("home");
  }, []);

  const openStuckPage = useCallback((request: StuckRequest) => {
    setStuckRequest(request);
    setView("stuck");
  }, []);

  // a press from another window: the companion writes the request and
  // raises this window; the storage event delivers it (or mount does, when
  // the main window wasn't up yet). the tray comes through a shell event.
  useEffect(() => {
    const pending = readStuckRequest(window.localStorage);
    if (pending) openStuckPage(pending);
    const onStorage = (e: StorageEvent) => {
      if (e.key !== STUCK_REQUEST_KEY || !e.newValue) return;
      const request = readStuckRequest(window.localStorage);
      if (request) openStuckPage(request);
    };
    window.addEventListener("storage", onStorage);
    let unlisten: (() => void) | undefined;
    let gone = false;
    if (inTauri()) {
      void listen<{ surface?: string }>("chordial:stuck", (event) => {
        const surface = (event.payload?.surface ?? "tray") as StuckSurface;
        openStuckPage(makeRequest(surface));
      })
        .then((un) => {
          if (gone) un();
          else unlisten = un;
        })
        .catch(() => undefined);
    }
    return () => {
      gone = true;
      window.removeEventListener("storage", onStorage);
      unlisten?.();
    };
  }, [openStuckPage]);

  const refreshCouncil = useCallback(() => {
    if (!token) return;
    fetchCouncil(token)
      .then((body) => setCouncil(body.council))
      .catch((e) => {
        if (isAuthError(e)) onAuthLost();
      });
  }, [token, onAuthLost]);

  useEffect(() => {
    refreshCouncil();
  }, [refreshCouncil]);

  useEffect(() => {
    if (!token) return;
    void resolveDailyRoom(token);
    const socket = new RoomSocket(token, {
      onPayload: (payload) => {
        bus.emitPayload(payload);
        if (!payload.ephemeral && payload.author_type !== "user") announceTasksChanged();
        // a daily-room council line landing while today's room isn't the
        // watched view becomes the door's nudge (room-aware: cycle lines
        // belong to a different door, mirrored phone lines are the
        // person's own words). an unrecognized room id gets ONE
        // re-resolve - midnight mints a new daily room.
        const verdict = classifyForNudge(
          payload, dailyRoomRef.current, viewRef.current);
        if (verdict === "nudge") {
          setUnread((n) => n + 1);
        } else if (verdict === "resolve") {
          void resolveDailyRoom(token).then(() => {
            if (classifyForNudge(payload, dailyRoomRef.current,
                                 viewRef.current, true) === "nudge") {
              setUnread((n) => n + 1);
            }
          });
        }
      },
      onStatus: (status) => {
        setSocketStatus(status);
        if (status === "revoked") onAuthLost();
      },
      onReconnect: () => {
        bus.emitReconnect();
        announceTasksChanged();
      },
    });
    socket.start();

    // the presence heartbeat (7a): the sidecar's OS idle clock, reported
    // over the socket so the server can route proactive words to the desk
    // or the phone. no sidecar (or a stale collector) reports openness
    // alone - the server treats that as active, the best signal available.
    let cancelled = false;
    const beat = async () => {
      let idle: number | undefined;
      try {
        const state = await fetchSidecarState();
        if (state.activity?.fresh) idle = state.activity.idle_seconds;
      } catch {
        idle = undefined;
      }
      if (!cancelled) {
        socket.send({ type: "presence", idle_seconds: idle });
      }
    };
    void beat();
    const timer = setInterval(() => void beat(), PRESENCE_BEAT_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
      socket.stop();
    };
  }, [token, bus, onAuthLost, resolveDailyRoom]);

  // stepping into TODAY'S room clears its door's nudge - a cycle room is a
  // different door and must not swallow the daily count
  useEffect(() => {
    if (view === "room") setUnread(0);
  }, [view]);

  if (!sessionLoaded) {
    // a single quiet frame while the keychain answers
    return null;
  }

  if (!token) {
    return (
      <LinkScreen
        onLinked={(newToken, deviceId) => {
          void storeSession(newToken, deviceId);
          setToken(newToken);
        }}
      />
    );
  }

  return (
    <div className="shell">
      {/* the stuck page shows one thing: no rail, no doors (§2). the
          window's own close is the only way out of thinking, and the
          page's own controls are the way out of a card. */}
      {view !== "stuck" && (
        <PresenceRail council={council} view={view} onNavigate={setView} />
      )}
      <main className="stage">
        {view === "home" && (
          <Home
            token={token}
            unread={unread}
            onEnterRoom={() => setView("room")}
            onEnterCycleRoom={(handle) => {
              setCycleRoom(handle);
              setView("cycle");
            }}
            onOpenArchive={(room) => {
              setArchiveRoom(room);
              setView("archive");
            }}
            onStuck={() => openStuckPage(makeRequest("home"))}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "stuck" && stuckRequest && (
          <StuckPage
            key={stuckRequest.request_id}
            token={token}
            request={stuckRequest}
            onClose={() => {
              setStuckRequest(null);
              setView("home");
            }}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "room" && (
          <Room
            token={token}
            council={council}
            bus={bus}
            socketStatus={socketStatus}
            onTurnComplete={refreshCouncil}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "cycle" && cycleRoom && (
          <Room
            key={cycleRoom.id}
            token={token}
            council={council}
            bus={bus}
            socketStatus={socketStatus}
            cycleRoom={cycleRoom}
            onBack={() => setView("home")}
            onTurnComplete={refreshCouncil}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "archive" && archiveRoom && (
          <ArchiveRoom
            token={token}
            council={council}
            room={archiveRoom}
            onEnterToday={() => setView("room")}
            onBack={() => setView("home")}
            onAuthLost={onAuthLost}
          />
        )}
      </main>
    </div>
  );
}

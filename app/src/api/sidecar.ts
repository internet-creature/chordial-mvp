// the deer window's line to the sidecar: loopback http + a push websocket.
// no bearer auth here - the sidecar binds 127.0.0.1 and gates browsers by
// exact origin; the handshake below is how it RECEIVES the device
// credential (for its outbox sync up to the chordial server).

export const SIDECAR_URL: string =
  import.meta.env.VITE_CHORDIAL_SIDECAR ?? "http://localhost:8485";

/** the task-clock model: one task's clock runs at a time, time BANKS.
 * `banked` is today's closed-run seconds per task id (json keys are
 * strings); the running clock's seconds are NOT in it until it pauses.
 * `run_seconds` is CREDITED time (wall minus applied rewind excisions);
 * `frozen` lists unbanked runs stopped by an unresolved correction. */
export interface FocusState {
  running: boolean;
  /** the active run's sidecar id - resolutions riding a transition must
   * belong to it (a held run's question resolves on the card instead) */
  run_id?: number;
  task_id?: number | null;
  label?: string | null;
  target_minutes?: number;
  started_at?: string;
  run_seconds?: number;
  excised_seconds?: number;
  over_target?: boolean;
  /** a run started from the stuck page (docs/STUCK_MODE_DESIGN.md §4):
   * the window renders the boundary exit from this, not from the line */
  run_mode?: "stuck" | null;
  episode_id?: string | null;
  execution_id?: string | null;
  /** the boundary's persisted answer: "keep_going" once chosen (the
   * other exit ends the run). null until the person chooses. */
  boundary_choice?: "keep_going" | null;
  banked: Record<string, number>;
  frozen?: FrozenRun[];
}

/** the stuck page's typed handoff: the sidecar deduplicates on it */
export interface StuckHandoff {
  execution_id: string;
  episode_id: string;
}

/** how a pause ends the run: a plain pause, or the stuck run's "stop
 * here" at its boundary (its own line, its own name in the ledger) */
export type PauseReason = "paused" | "boundary";

export interface FrozenRun {
  run_id: number;
  task_id?: number | null;
  label?: string | null;
  frozen_at: string;
  frozen_reason: string;
  run_seconds: number;
}

/** one correction boundary, impact-framed by the sidecar at render time -
 * amounts are derived fresh, never persisted (docs/REWIND_DESIGN.md §5) */
export interface RewindCandidate {
  at: string;
  kind: string;
  rank: number;
  removed_seconds: number;
  credited_seconds: number;
}

/** the open correction question (docs/REWIND_DESIGN.md §6): at most one
 * surfaces at a time; `frozen` means its run already stopped and the
 * answer will bank it */
export interface RewindOffer {
  offer_uuid: string;
  run_id: number;
  task_id?: number | null;
  label?: string | null;
  frozen: boolean;
  frozen_reason?: string | null;
  contested_seconds: number;
  candidates: RewindCandidate[];
}

/** a resolution riding a transition (the combined buttons): resolve the
 * question atomically, then pause/finish/switch */
export interface Resolution {
  offer_uuid: string;
  action: "remove" | "keep";
  at?: string;
}

/** derived activity flags only - the sidecar never surfaces bundle ids */
export interface ActivityFlags {
  fresh: boolean;
  idle_seconds: number;
  blocked: boolean;
  drifting: boolean;
}

/** the away episode (docs/STUCK_MODE_DESIGN.md §4 + §4.1): the step
 * waiting underneath while they're off the desk; `returned_at` is the
 * one real return - the re-offer shows from it, hushed deer or not */
export interface AwayState {
  execution_id: string;
  episode_id: string;
  task_id: number | null;
  label: string | null;
  next_action: string | null;
  minutes: number | null;
  opened_at: string;
  departed_at: string | null;
  returned_at: string | null;
  seconds_away: number;
  phase: "present" | "possibly_away" | "away" | "returned";
}

export interface SidecarState {
  focus: FocusState;
  activity?: ActivityFlags;
  offer?: RewindOffer | null;
  line: string | null;
  linked: boolean;
  sync_error: string | null;
  away?: AwayState | null;
}

export type SidecarPush =
  | {
      type: "state";
      focus: FocusState;
      activity?: ActivityFlags;
      offer?: RewindOffer | null;
      away?: AwayState | null;
    }
  | { type: "line"; moment: string; text: string }
  | { type: "rewind_offer"; offer: RewindOffer | null };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${SIDECAR_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const message =
      body && typeof body.error === "string"
        ? body.error
        : `sidecar request failed (${resp.status})`;
    throw new Error(message);
  }
  return body as T;
}

export const fetchSidecarState = () => request<SidecarState>("/v1/state");

export const handshake = (token: string, serverUrl: string) =>
  request<{ ok: boolean }>("/v1/handshake", {
    method: "POST",
    body: JSON.stringify({ token, server_url: serverUrl }),
  });

/** start the clock on a task - if another task's clock is running, this IS
 * the switch (the old run banks first) */
export const startFocus = (
  taskId: number | null,
  label: string,
  targetMinutes: number,
  resolution?: Resolution,
  execution?: StuckHandoff,
) =>
  request<{
    focus: FocusState;
    line: string;
    offer?: RewindOffer | null;
    /** the same execution again: the live run answered, nothing started */
    replayed?: boolean;
  }>("/v1/focus/start", {
    method: "POST",
    body: JSON.stringify({
      task_id: taskId,
      label: label || null,
      target_minutes: targetMinutes,
      resolution,
      execution,
    }),
  });

interface TransitionResponse {
  focus: FocusState;
  run?: unknown;
  /** present when the run FROZE instead of banking: the clock stopped,
   * the question is still open (docs/REWIND_DESIGN.md §6) */
  held?: { frozen: boolean; run_id: number };
  offer?: RewindOffer | null;
  line: string;
}

export const pauseFocus = (resolution?: Resolution, reason?: PauseReason) =>
  request<TransitionResponse>("/v1/focus/pause", {
    method: "POST",
    body: JSON.stringify({
      ...(resolution ? { resolution } : {}),
      ...(reason ? { reason } : {}),
    }),
  });

/** the boundary's "keep going", persisted with the run so a reload
 * mid-overtime never asks again */
export const keepGoingAtBoundary = () =>
  request<{ focus: FocusState }>("/v1/focus/boundary", {
    method: "POST",
    body: JSON.stringify({ choice: "keep_going" }),
  });

/** a body proposal that leaves the desk: the clock pauses, the step waits */
export const startAway = (body: {
  execution: StuckHandoff;
  task_id?: number | null;
  label?: string | null;
  next_action?: string | null;
  minutes?: number | null;
}) =>
  request<{
    focus: FocusState;
    away: AwayState | null;
    line: string;
    replayed?: boolean;
  }>("/v1/away/start", { method: "POST", body: JSON.stringify(body) });

/** the re-offer answered: start the waiting step, or let it go */
export const resolveAway = (choice: "start" | "not_now") =>
  request<{ focus: FocusState; away: AwayState | null; line: string }>(
    "/v1/away/step",
    { method: "POST", body: JSON.stringify({ choice }) },
  );

/** a chordial surface came into (or left) view: presence as good as a
 * keystroke for the attention seam */
export const postAttention = (visible: boolean) =>
  request<{ ok: boolean }>("/v1/attention", {
    method: "POST",
    body: JSON.stringify({ visible }),
  }).catch(() => undefined);

export const finishFocus = (resolution?: Resolution) =>
  request<TransitionResponse>("/v1/focus/finish", {
    method: "POST",
    body: JSON.stringify(resolution ? { resolution } : {}),
  });

/** answer the open question in place (no transition): remove a candidate
 * boundary or keep all time. if the run was frozen, the answer banks it.
 * `offer` in the response is the NEXT authoritative open question -
 * another held block may still be waiting - so client state comes from
 * it, never from an assumed null. */
export const resolveRewind = (
  offerUuid: string,
  action: "remove" | "keep",
  at?: string,
) =>
  request<{
    focus: FocusState;
    resolved: { offer_uuid: string; status: string };
    offer: RewindOffer | null;
    run: { reason: string; seconds: number } | null;
    line: string | null;
  }>("/v1/focus/rewind", {
    method: "POST",
    body: JSON.stringify({ offer_uuid: offerUuid, action, at }),
  });

/** lift an applied excision - the tap may have been the mistake */
export const undoRewind = (offerUuid: string) =>
  request<{ focus: FocusState; offer: RewindOffer | null }>(
    "/v1/focus/rewind/undo",
    {
      method: "POST",
      body: JSON.stringify({ offer_uuid: offerUuid }),
    },
  );

const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 15000;

/** the push channel, with quiet reconnection - the deer window may outlive
 * many sidecar restarts and should just find her again. */
export class SidecarSocket {
  private handlers: {
    onPush: (payload: SidecarPush) => void;
    onStatus: (connected: boolean) => void;
  };
  private ws: WebSocket | null = null;
  private stopped = false;
  private backoffMs = BACKOFF_START_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(handlers: {
    onPush: (payload: SidecarPush) => void;
    onStatus: (connected: boolean) => void;
  }) {
    this.handlers = handlers;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  private connect(): void {
    if (this.stopped) return;
    const ws = new WebSocket(SIDECAR_URL.replace(/^http/, "ws") + "/v1/ws");
    this.ws = ws;
    ws.onopen = () => {
      this.backoffMs = BACKOFF_START_MS;
      this.handlers.onStatus(true);
    };
    ws.onmessage = (event) => {
      try {
        this.handlers.onPush(JSON.parse(event.data));
      } catch {
        // not ours to worry about
      }
    };
    ws.onclose = () => {
      this.ws = null;
      if (this.stopped) return;
      this.handlers.onStatus(false);
      this.reconnectTimer = setTimeout(() => this.connect(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
    };
  }
}

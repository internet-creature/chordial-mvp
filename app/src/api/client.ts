// the http side of the /api/v1 contract. every call carries the device
// bearer token; a 401 means the device was revoked (or the token is stale),
// which the app treats as "return to the link screen".

import type {
  ArcState,
  ArchivedRoom,
  CouncilMember,
  CycleDoorsPayload,
  CyclePayload,
  OpenCycleRoomResult,
  RoomCurrent,
  HistoryRow,
  SendResult,
  TaskPatch,
  TaskRow,
  TodayPayload,
} from "./types";
import { announceTasksChanged } from "../lib/taskSync";

// dev-mode: the python server runs beside `tauri dev` on its usual port.
// override with VITE_CHORDIAL_SERVER when the server lives elsewhere.
export const SERVER_URL: string =
  import.meta.env.VITE_CHORDIAL_SERVER ?? "http://localhost:8484";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** the device was revoked or the token is stale - time to re-link */
export const isAuthError = (e: unknown) =>
  e instanceof ApiError && e.status === 401;

async function request<T>(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<T> {
  const { token, ...rest } = init;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(rest.headers as Record<string, string> | undefined),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(`${SERVER_URL}${path}`, { ...rest, headers });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const message =
      body && typeof body.error === "string"
        ? body.error
        : `request failed (${resp.status})`;
    throw new ApiError(resp.status, message);
  }
  return body as T;
}

export function linkDevice(
  code: string,
  name: string,
): Promise<{ device_id: string; token: string }> {
  return request("/api/v1/devices/link", {
    method: "POST",
    body: JSON.stringify({ code: code.trim(), name }),
  });
}

export function fetchCouncil(
  token: string,
): Promise<{ council: CouncilMember[] }> {
  return request("/api/v1/council", { token });
}

export function fetchRoomCurrent(token: string): Promise<RoomCurrent> {
  return request("/api/v1/rooms/current", { token });
}

export function fetchRoomMessages(
  token: string,
  limit = 100,
): Promise<{ messages: HistoryRow[] }> {
  return request(`/api/v1/rooms/current/messages?limit=${limit}`, { token });
}

export async function fetchToday(
  token: string,
  signal?: AbortSignal,
): Promise<TodayPayload> {
  // A stalled connection must not hold the refresh queue forever. Use a
  // separate controller for each read, also cancelled when its view/session ends.
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timeout = setTimeout(abort, 15_000);
  signal?.addEventListener("abort", abort);
  if (signal?.aborted) abort();
  try {
    return await request("/api/v1/today", {
      token,
      signal: controller.signal,
      cache: "no-store",
    });
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

/** the shared cycle state: projection of baseline + scope changes + progress */
export function fetchCycle(token: string): Promise<CyclePayload> {
  return request("/api/v1/cycle", { token });
}

/** the journal: recent daily rooms, newest first, summaries where committed */
export function fetchArchive(
  token: string,
): Promise<{ rooms: ArchivedRoom[] }> {
  return request("/api/v1/rooms", { token });
}

/** a past room's transcript - read-only, remembering not writing */
export function fetchArchivedMessages(
  token: string,
  roomUuid: string,
): Promise<{ messages: HistoryRow[] }> {
  return request(`/api/v1/rooms/${roomUuid}/messages`, { token });
}

/** the cycle-room doors: the latest sealed cycle + its rooms (phase 6b) */
export function fetchCycleDoors(token: string): Promise<CycleDoorsPayload> {
  return request("/api/v1/rooms/cycle", { token });
}

/** open (get-or-create) the retro room; edwin presents the card on create */
export function openCycleRetro(token: string): Promise<OpenCycleRoomResult> {
  return request("/api/v1/rooms/cycle/retro", { method: "POST", token });
}

/** open (get-or-create) the planning room following the sealed cycle */
export function openCyclePlanning(token: string): Promise<OpenCycleRoomResult> {
  return request("/api/v1/rooms/cycle/planning", { method: "POST", token });
}

/** the arc's posture: how much quiet the scorecard history has earned */
export function fetchArc(token: string): Promise<{ arc: ArcState }> {
  return request("/api/v1/arc", { token });
}

/** quick-add from the deer window: title only, lands scheduled-today */
export function createTask(
  token: string,
  title: string,
): Promise<{ ok: boolean; task: TaskRow }> {
  return request<{ ok: boolean; task: TaskRow }>("/api/v1/tasks", {
    method: "POST",
    token,
    body: JSON.stringify({ title }),
  }).then(taskChanged);
}

export function setTaskStatus(
  token: string,
  taskId: number,
  status: string,
): Promise<{ ok: boolean; task: TaskRow }> {
  return request<{ ok: boolean; task: TaskRow }>(
    `/api/v1/tasks/${taskId}/status`,
    {
      method: "POST",
      token,
      body: JSON.stringify({ status }),
    },
  ).then(taskChanged);
}

/** the shaping seam (docs/FOCUS_DOGFOOD_DESIGN.md §4): scope, reschedule,
 * status, set aside - any subset in one body. never touches the sidecar
 * clock; the window pauses first when it must. */
export function patchTask(
  token: string,
  taskId: number,
  patch: TaskPatch,
): Promise<{ ok: boolean; task: TaskRow }> {
  return request<{ ok: boolean; task: TaskRow }>(`/api/v1/tasks/${taskId}`, {
    method: "PATCH",
    token,
    body: JSON.stringify(patch),
  }).then(taskChanged);
}

function taskChanged<T>(result: T): T {
  announceTasksChanged();
  return result;
}

// a turn can be slow (the model is thinking) and a duplicate POST for the
// same client_message_id while the first is in flight gets a 409 - wait and
// re-ask; the receipt layer replays the finished answer without a second turn.
const IN_FLIGHT_RETRY_MS = 2500;
const IN_FLIGHT_RETRIES = 8;

export async function sendRoomMessage(
  token: string,
  text: string,
  clientMessageId: string,
  roomUuid?: string,
): Promise<SendResult> {
  const path = roomUuid
    ? `/api/v1/rooms/${roomUuid}/messages`
    : "/api/v1/rooms/current/messages";
  for (let attempt = 0; ; attempt++) {
    try {
      const result = await request<SendResult>(path, {
        method: "POST",
        token,
        body: JSON.stringify({
          text,
          client_message_id: clientMessageId,
        }),
      });
      announceTasksChanged();
      return result;
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status === 409 &&
        attempt < IN_FLIGHT_RETRIES
      ) {
        await new Promise((r) => setTimeout(r, IN_FLIGHT_RETRY_MS));
        continue;
      }
      throw e;
    }
  }
}

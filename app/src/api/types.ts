// the /api/v1 contract, as the app consumes it. shapes mirror
// src/web/server.py handlers - if a field changes there, it changes here.

export interface CouncilMember {
  id: string;
  chair: boolean;
  emoji: string;
  lane: string;
  specialty: string;
  /** the name the user knows them by: persona override or authored id */
  name: string;
  /** override form if reshaped, else the authored species */
  species: string;
  status: "not_met" | "introducing" | "active" | "declined" | "disabled";
}

export interface RoomInfo {
  id: string;
  type: string;
  date: string | null;
  status: string;
}

export interface RoomCurrent {
  room: RoomInfo;
  chat_available: boolean;
}

/** a persisted room message (GET /rooms/current/messages) */
export interface HistoryRow {
  id: number;
  author: string;
  author_type: string;
  content: string;
  at: string | null;
  platform: string | null;
}

/** a delivered line (websocket payload / POST response message) */
export interface DeliveryPayload {
  type: string;
  id?: string;
  author: string;
  content: string;
  at?: string;
  ephemeral?: boolean;
  /** which room the line belongs to (phase 6b); absent on older payload
   * shapes, which only ever carried daily-room lines */
  room?: string;
  /** the tether mirror (phase 7a): "user" when this is the person's own
   * line echoed from another platform; absent on council lines */
  author_type?: string;
  /** where a mirrored line originated (e.g. "telegram"); absent on
   * app-first deliveries */
  platform?: string;
}

export interface SendResult {
  ok: boolean;
  messages: DeliveryPayload[];
  replayed?: boolean;
}

export interface TaskRow {
  id: number;
  public_id: string;
  title: string;
  status: string;
  priority: string | null;
  scheduled: string | null;
  window: string | null;
  pom_estimate: number | null;
  plan_title: string | null;
  helper: string | null;
  description: string | null;
  /** the scope: the one-line first piece a block runs on (§3) */
  next_action: string | null;
  /** the user-local date this task was parked for, or null (§2) */
  set_aside_on: string | null;
  /** the evidence nudge (§10.2): this row keeps waiting - moved twice,
   *  parked on two days, short starts without a scope, untouched two days.
   *  server-computed; the chip that renders it is the ask-pip slice */
  needs_breakdown?: boolean;
}

/** PATCH /api/v1/tasks/{id}: any subset; unknown keys are refused */
export interface TaskPatch {
  next_action?: string | null;
  scheduled?: string | null;
  status?: string;
  set_aside?: boolean;
}

/** a finished-today row: a lighter shape than TaskRow (no plan joins) */
export interface DoneTaskRow {
  id: number;
  title: string;
  status: string;
  pom_estimate: number | null;
  priority: string | null;
  scheduled: string | null;
  closed_at: string;
  plan_title: string | null;
}

/** a commitment inside the cycle projection (GET /api/v1/cycle) */
export interface CommitmentRow {
  id: number;
  public_id: string;
  uuid: string;
  title: string;
  status: string;
  priority: string | null;
  blocks_planned: number | null;
  baseline_blocks: number | null;
  blocks_done: number;
  seconds_done: number;
  next_action: string | null;
  plan_id: number | null;
  plan_title: string | null;
  task_id: number | null;
  task_title: string | null;
}

/** the cycle view projection: baseline + scope changes + progress.
 * cycle is null when no cycle is active. */
export interface CyclePayload {
  cycle: {
    id: number;
    public_id: string;
    title: string;
    status: string;
    theme: string | null;
    capacity_blocks: number | null;
    start_date: string | null;
    end_date: string | null;
  } | null;
  frozen?: boolean;
  frozen_at?: string | null;
  commitments?: CommitmentRow[];
  scope_changes?: {
    id: number;
    commitment_uuid: string | null;
    reason: string;
    deltas: Record<string, unknown>;
    created_at: string | null;
  }[];
  totals?: {
    capacity_blocks: number | null;
    planned_blocks: number;
    done_blocks: number;
    unattributed_seconds: number;
    unallocated_blocks: number | null;
  };
}

/** an archived (or still-open) day in the journal (GET /api/v1/rooms) */
export interface ArchivedRoom {
  id: number;
  room_uuid: string;
  room_type: string;
  status: string;
  date: string | null;
  /** the cycle-room anchor (phase 6b); null on daily/legacy rooms */
  subject_type?: string | null;
  subject_id?: string | null;
  summary: string | null;
  /** contractual one-liner (message counts) - safe to show in lists,
   * unlike the free-text digest whose tail quotes conversation */
  summary_line: string | null;
}

/** a cycle room as the doors speak it (phase 6b) */
export interface CycleRoomInfo {
  id: string;
  type: "cycle_retro" | "cycle_planning";
  status: string;
  subject_id: string | null;
}

export interface SealedCycleBrief {
  id: number;
  public_id: string;
  title: string;
  theme: string | null;
  start_date: string | null;
  end_date: string | null;
  closed_at: string | null;
}

/** GET /api/v1/rooms/cycle - doors is null while no cycle has sealed */
export interface CycleDoorsPayload {
  doors: {
    cycle: SealedCycleBrief;
    scored: boolean;
    retro: CycleRoomInfo | null;
    planning: CycleRoomInfo | null;
  } | null;
  chat_available: boolean;
}

/** GET /api/v1/arc - the taper's read model (phase 6c): the arc posture
 * and the check-in beat the scorecard history has earned */
export interface ArcState {
  posture: "settling in" | "building" | "rhythm" | "keeping watch";
  streak: number;
  multiplier: number;
  base_minutes: number;
  checkin_minutes: number;
  judged: {
    subject_id: string | null;
    title: string;
    verdict: "steady" | "wobbly" | "unjudged";
    reasons: string[];
  }[];
}

/** POST /api/v1/rooms/cycle/{retro|planning} */
export interface OpenCycleRoomResult {
  room: CycleRoomInfo;
  cycle: SealedCycleBrief;
  created: boolean;
}

export interface TodayPayload {
  today: string;
  user: { name: string | null };
  pom_minutes: number;
  break_minutes: number;
  buckets: {
    overdue: TaskRow[];
    today: TaskRow[];
    in_progress: TaskRow[];
    done: DoneTaskRow[];
    /** open tasks parked for today - out of the live buckets (§2) */
    set_aside: TaskRow[];
  };
  focus: {
    active_task_id: number | null;
    seconds: Record<string, number>;
  };
  server_time: string;
}

// --- stuck mode (docs/STUCK_MODE_DESIGN.md §5.1) ----------------------------

export type StuckSurface = "companion" | "home" | "tray" | "room" | "telegram";
export type StuckKind =
  | "step" | "switch" | "thread" | "body" | "sound" | "rest" | "company";
export type StuckAction =
  | "start_task" | "pause_and_away" | "body_here" | "rest_here"
  | "start_company" | "point_to_sound";
export type StuckStatus =
  | "thinking" | "ready" | "fallback" | "failed" | "accepted" | "rested" | "closed";
export type StuckReaction = "accepted" | "different" | "too_much" | "closed";

export interface StuckProposal {
  proposal_id: string;
  generation: number;
  source: "model" | "fallback";
  kind: StuckKind;
  action: StuckAction;
  line: string;
  enough: string;
  minutes: number | null;
  why: string | null;
  why_register: "matters" | "soft";
  thing: { task_id?: number; plan_id?: number; label?: string } | null;
  prepared_step: { task_id: number; next_action: string } | null;
  rationale_codes: string[];
}

/** the typed handoff the sidecar deduplicates on (§4) */
export interface StuckExecution {
  execution_id: string;
  episode_id: string;
  action: StuckAction;
  kind: StuckKind;
  task_id: number | null;
  next_action: string | null;
  minutes: number | null;
  line: string;
}

export interface StuckEpisode {
  episode_id: string;
  status: StuckStatus;
  generation: number;
  surface: StuckSurface;
  task_id: number | null;
  source: "model" | "fallback" | null;
  /** the current generation's proposals, in the turn's order */
  proposals: StuckProposal[];
  rejected_proposal_ids: string[];
  rejected_kinds: StuckKind[];
  exhausted: boolean;
  accepted_proposal_id: string | null;
  execution: StuckExecution | null;
  error: string | null;
  opened_at: string | null;
  ready_at: string | null;
  closed_at: string | null;
}

export type StuckOutcome =
  | "replay" | "recorded" | "regenerate" | "exhausted"
  | "accepted" | "rested" | "closed";

export interface StuckOpenResult {
  ok: boolean;
  episode: StuckEpisode;
  replayed: boolean;
}

export interface StuckReactResult {
  ok: boolean;
  episode: StuckEpisode;
  outcome: StuckOutcome;
  execution?: StuckExecution;
}

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  createTask,
  patchTask,
  SERVER_URL,
  setTaskStatus,
} from "../api/client";
import {
  fetchSidecarState,
  finishFocus,
  handshake,
  pauseFocus,
  resolveRewind,
  SidecarSocket,
  startFocus,
  undoRewind,
  type ActivityFlags,
  type FocusState,
  type Resolution,
  type RewindOffer,
} from "../api/sidecar";
import type { DoneTaskRow, TaskRow } from "../api/types";
import { useToday } from "../lib/useToday";
import TaskSyncStatus from "./TaskSyncStatus";
import {
  autoBarEnabled,
  barPositionFrom,
  clampToArea,
  denPositionFrom,
  FORM_SIZES,
  formFor,
  lastTarget,
  loadFormPosition,
  loadLastForm,
  rememberTarget,
  saveFormPosition,
  saveLastForm,
  setAutoBar,
  targetChoices,
  type Form,
  type Point,
} from "../lib/companion";
import {
  addPendingDone,
  listPendingDone,
  removePendingDone,
} from "../lib/pendingDone";
import {
  altLabel,
  amountLabel,
  appliedLabel,
  chipLabel,
  frozenVerb,
  quietLine,
  removeLabel,
} from "../lib/rewind";
import {
  dayStatus,
  dismissSaying,
  lastSayingText,
  loadSaying,
  nextSeq,
  saveSaying,
  type Saying,
} from "../lib/bubble";
import {
  rememberScopeSkip,
  runLabel,
  SCOPE_CAP,
  SCOPE_COPY,
  scopeSkipped,
  splitLabel,
  tomorrowOf,
} from "../lib/scope";
import {
  loadSession,
  SESSION_REV_KEY,
  TOKEN_STORAGE_KEY,
} from "../lib/session";
import {
  hideWindow,
  isAlwaysOnTop,
  moveWindow,
  resizeWindow,
  setAlwaysOnTop,
  windowPosition,
  workArea,
} from "../lib/tauriWindow";
import { useLeafFlourish } from "./LeafFlourish";
import InlineContent from "./InlineContent";

// the bar has one text slot: a fresh saying borrows it this long, then
// the running task's title has it back. the saying itself stays in the
// bubble until the next one or a dismiss - nothing times it out.
const BAR_LINE_MS = 12000;
// a chrome notice (an action that did not go through) is brief
const NOTICE_MS = 8000;
const TOKEN_POLL_MS = 2500;
const PENDING_RETRY_MS = 10000;
// the card collapses to the quiet chip after this - deferral, not expiry
// (docs/REWIND_DESIGN.md: the question never dies by timer)
const OFFER_CARD_COLLAPSE_MS = 45000;
const APPLIED_LINGER_MS = 20000;

function mmss(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.max(0, Math.round(totalSeconds % 60));
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** the companion window (docs/FOCUS_DOGFOOD_DESIGN.md §1, §11): the
 * day's tasks with their banked-time bars, one running clock, and a
 * small creature keeping you company. tasks are canonical on the server;
 * the clock and the banked minutes live in the sidecar; this window is
 * where they meet. it wears two forms: the DEN (everything) while no
 * clock runs, and the slim BAR while one does. */
export default function DeerWindow() {
  // reactive, not read-once: the person links in the MAIN window while
  // this one is already open. the cross-window storage event on the rev
  // counter is the fast path (the token itself lives in the keychain in
  // packaged builds - no secret rides the event); a slow poll is the belt
  // (webview storage-event delivery varies)
  const [token, setToken] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusState>({
    running: false,
    banked: {},
  });
  const [pendingDone, setPendingDone] = useState<number[]>(() =>
    listPendingDone(window.localStorage),
  );
  const [activity, setActivity] = useState<ActivityFlags | null>(null);
  const { today, error: taskError, updatedAt, refreshToday } = useToday(token);
  // what the companion last said, and a chrome notice: two different
  // things, two different homes (the bubble, the status row)
  const [saying, setSaying] = useState<Saying | null>(() =>
    loadSaying(window.sessionStorage),
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [newTitle, setNewTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmingFinish, setConfirmingFinish] = useState(false);
  const [confirmingPause, setConfirmingPause] = useState(false);
  const { flourish, leaves } = useLeafFlourish();
  const [offer, setOffer] = useState<RewindOffer | null>(null);
  const [cardExpanded, setCardExpanded] = useState(false);
  const [showAlt, setShowAlt] = useState(false);
  const [applied, setApplied] = useState<{
    offerUuid: string;
    removed: number;
    credited: number;
  } | null>(null);
  // select, then start: a click opens a row; only ▸ / start run the clock
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [chipMinutes, setChipMinutes] = useState<number | null>(null);
  // the scope (§3): the draft in the form, and whether an existing scope
  // is being re-written in place
  const [scopeDraft, setScopeDraft] = useState("");
  const [editingScope, setEditingScope] = useState(false);
  // the parked section (§2): which set-aside row is open
  const [asideId, setAsideId] = useState<number | null>(null);
  // the forms: den <-> bar, remembered preference, and "peeking" = the
  // den opened from the bar without stopping the clock
  const [autoBar, setAutoBarState] = useState(() =>
    autoBarEnabled(window.localStorage),
  );
  const [peeking, setPeeking] = useState(false);
  const [onTop, setOnTop] = useState<boolean | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cardTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const appliedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // the window geometry runner: one switch at a time, in order
  const geometry = useRef<Promise<void>>(Promise.resolve());
  const wornForm = useRef<Form | null>(null);

  /** the companion said something: it shows until replaced or dismissed */
  const say = useCallback((text: string | null | undefined) => {
    if (!text) return;
    setSaying((previous) => {
      const next = { seq: nextSeq(previous, Date.now()), text };
      saveSaying(window.sessionStorage, next);
      return next;
    });
  }, []);

  const dismiss = useCallback(() => {
    setSaying(null);
    dismissSaying(window.sessionStorage);
  }, []);

  /** chrome speaking, not the companion: a failed action, briefly */
  const showNotice = useCallback((text: string) => {
    setNotice(text);
    if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_MS);
  }, []);

  /** open the card; after a while it settles into the chip - the person's
   * regained thread is never held hostage by retrospective accounting */
  const expandCard = useCallback(() => {
    setCardExpanded(true);
    if (cardTimer.current !== null) clearTimeout(cardTimer.current);
    cardTimer.current = setTimeout(
      () => setCardExpanded(false),
      OFFER_CARD_COLLAPSE_MS,
    );
  }, []);

  /** an answered question leaves no controls behind */
  const clearOfferUi = useCallback(() => {
    setCardExpanded(false);
    setShowAlt(false);
    setConfirmingPause(false);
  }, []);

  // notice link/relink/revoke from the main window: storage event + poll.
  // the async load keeps arrivals in order by only applying a changed value
  const refreshToken = useCallback(() => {
    void loadSession().then((fresh) => {
      setToken((current) => (fresh === current ? current : fresh));
    });
  }, []);

  useEffect(() => {
    refreshToken(); // the initial read (keychain or localStorage)
    const onStorage = (e: StorageEvent) => {
      if (
        e.key === SESSION_REV_KEY ||
        e.key === TOKEN_STORAGE_KEY ||
        e.key === null
      ) {
        refreshToken();
      }
    };
    window.addEventListener("storage", onStorage);
    const poll = setInterval(refreshToken, TOKEN_POLL_MS);
    return () => {
      window.removeEventListener("storage", onStorage);
      clearInterval(poll);
    };
  }, [refreshToken]);

  // hand the sidecar its credential whenever we have one AND can reach it -
  // covers first link, relink, and a sidecar that started after this window
  useEffect(() => {
    if (token && connected) {
      handshake(token, SERVER_URL).catch(() => {});
    }
  }, [token, connected]);

  useEffect(() => {
    fetchSidecarState()
      .then((state) => {
        setFocus(state.focus);
        if (state.activity) setActivity(state.activity);
        // the sidecar repeats its latest line on connect: a line already
        // shown (or dismissed) is not said again
        if (state.line && state.line !== lastSayingText(window.sessionStorage))
          say(state.line);
        setOffer(state.offer ?? null);
      })
      .catch(() => {});
  }, [token, connected]);

  useEffect(() => {
    const socket = new SidecarSocket({
      onPush: (payload) => {
        if (payload.type === "state") {
          setFocus(payload.focus);
          if (payload.activity) setActivity(payload.activity);
          if ("offer" in payload) setOffer(payload.offer ?? null);
        }
        if (payload.type === "line") say(payload.text);
        if (payload.type === "rewind_offer") {
          // the return moment: the card IS the welcome
          setOffer(payload.offer);
          if (payload.offer) expandCard();
        }
      },
      onStatus: setConnected,
    });
    socket.start();
    return () => socket.stop();
  }, [say, expandCard]);

  // a question that resolved elsewhere takes its controls with it
  useEffect(() => {
    if (offer === null) clearOfferUi();
  }, [offer, clearOfferUi]);

  useEffect(
    () => () => {
      if (cardTimer.current !== null) clearTimeout(cardTimer.current);
      if (appliedTimer.current !== null) clearTimeout(appliedTimer.current);
      if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    },
    [],
  );

  // the offline-finish ledger: retry canonical done-mutations until the
  // server confirms them; the rows render as "syncing" in the meantime
  useEffect(() => {
    if (!token || pendingDone.length === 0) return;
    let cancelled = false;

    const flush = async () => {
      let changed = false;
      for (const taskId of listPendingDone(window.localStorage)) {
        try {
          await setTaskStatus(token, taskId, "done");
          removePendingDone(window.localStorage, taskId);
          changed = true;
        } catch (err) {
          // a 404 means the task is gone - nothing left to complete
          if (err instanceof ApiError && err.status === 404) {
            removePendingDone(window.localStorage, taskId);
            changed = true;
          }
        }
      }
      if (cancelled) return;
      setPendingDone(listPendingDone(window.localStorage));
      if (changed) refreshToday();
    };

    flush();
    const timer = setInterval(flush, PENDING_RETRY_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [token, pendingDone.length, refreshToday]);

  // one local second-hand while a clock runs
  useEffect(() => {
    if (!focus.running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [focus.running]);

  // a stopped clock ends the peek - the next block starts in the bar
  useEffect(() => {
    if (!focus.running) setPeeking(false);
  }, [focus.running]);

  // the window's own controls: read the on-top state once
  useEffect(() => {
    isAlwaysOnTop()
      .then((value) => setOnTop(value))
      .catch(() => {});
  }, []);

  const form = formFor({ running: focus.running, autoBar, peeking });

  // the form switch (§11.1): each form keeps its own place on the screen.
  // the switch saves where the OLD form was, resizes, and moves to where
  // the new form last sat (or, the first time, along the old form's
  // bottom edge, so the bar appears where the deer's feet were). on
  // mount the den size is enforced - the window-state plugin restores
  // whatever rect was current at exit, bar included - and the den is
  // moved only when the exit was in bar form (otherwise the plugin's
  // restore is the freshest place). every destination is clamped into
  // the monitor's work area: the bar is wider than the den, and a
  // remembered place may belong to a screen that's gone. serialised
  // through a promise chain so a quick flip-back never interleaves two
  // resizes.
  useEffect(() => {
    geometry.current = geometry.current
      .then(async () => {
        const prev = wornForm.current;
        if (prev === form) return;
        const settle = async (target: Point | null) => {
          const area = await workArea();
          return target && area
            ? clampToArea(target, FORM_SIZES[form], area)
            : target;
        };
        if (prev === null) {
          const exitedIn = loadLastForm(window.localStorage);
          await resizeWindow(FORM_SIZES[form]);
          if (exitedIn === "bar" && form === "den") {
            const here = await windowPosition();
            const target = await settle(
              loadFormPosition(window.localStorage, "den") ??
                (here
                  ? denPositionFrom(here, FORM_SIZES.den, FORM_SIZES.bar)
                  : null),
            );
            if (target) await moveWindow(target);
          }
        } else {
          const here = await windowPosition();
          if (here) saveFormPosition(window.localStorage, prev, here);
          let target = loadFormPosition(window.localStorage, form);
          if (!target && here) {
            target =
              form === "bar"
                ? barPositionFrom(here, FORM_SIZES.den, FORM_SIZES.bar)
                : denPositionFrom(here, FORM_SIZES.den, FORM_SIZES.bar);
          }
          target = await settle(target);
          await resizeWindow(FORM_SIZES[form]);
          if (target) await moveWindow(target);
        }
        wornForm.current = form;
        saveLastForm(window.localStorage, form);
      })
      .catch(() => {
        // geometry is cosmetic; the clock and the list never depend on it
        wornForm.current = form;
      });
  }, [form]);

  const pomMinutes = today?.pom_minutes ?? 25;
  // the clock shows CREDITED time: wall minus applied rewind excisions -
  // an applied correction visibly moves it back
  const runSeconds =
    focus.running && focus.started_at
      ? Math.max(
          0,
          (now - Date.parse(focus.started_at)) / 1000 -
            (focus.excised_seconds ?? 0),
        )
      : 0;
  const targetSeconds = (focus.target_minutes ?? pomMinutes) * 60;
  const overtime = focus.running && runSeconds >= targetSeconds;

  const openTasks: TaskRow[] = today
    ? [
        ...today.buckets.in_progress,
        ...today.buckets.today,
        ...today.buckets.overdue,
      ]
    : [];
  const doneTasks: DoneTaskRow[] = today?.buckets.done ?? [];
  const asideTasks: TaskRow[] = today?.buckets.set_aside ?? [];
  const todayIso = today?.today ?? null;
  // the running task's canonical title, from whichever list holds it -
  // titles may contain ": " themselves, so the label is split against it
  const runningTitle =
    typeof focus.task_id === "number"
      ? ([...openTasks, ...asideTasks, ...doneTasks].find(
          (t) => t.id === focus.task_id,
        )?.title ?? null)
      : null;
  const runningLabel = focus.label
    ? splitLabel(focus.label, runningTitle)
    : null;
  const minutesToday = Math.floor(
    (Object.values(focus.banked).reduce((a, b) => a + b, 0) + runSeconds) / 60,
  );

  /** the scoping form is owed when the task has no scope and "just start"
   * wasn't chosen for it today (per-viewer memory; tomorrow it asks again) */
  function needsScope(task: TaskRow): boolean {
    if (task.next_action) return false;
    if (!todayIso) return false;
    return !scopeSkipped(window.localStorage, task.id, todayIso);
  }

  /** today's minutes on a task, live: banked runs + the running clock */
  function taskSeconds(taskId: number): number {
    const banked = focus.banked[String(taskId)] ?? 0;
    return focus.running && focus.task_id === taskId
      ? banked + runSeconds
      : banked;
  }

  function taskFill(task: TaskRow | DoneTaskRow): number {
    const target = Math.max(task.pom_estimate ?? 1, 0.5) * pomMinutes * 60;
    return Math.min(1, taskSeconds(task.id) / target);
  }

  /** a click opens the row (or closes it again); nothing starts */
  function selectTask(task: TaskRow) {
    if (selectedId === task.id) {
      setSelectedId(null);
      return;
    }
    setSelectedId(task.id);
    setChipMinutes(lastTarget(window.sessionStorage, task.id, pomMinutes));
    setScopeDraft(task.next_action ?? "");
    setEditingScope(false);
    setAsideId(null);
  }

  /** run the clock on a task - if another task's clock is running this
   * IS the switch (the old run banks first) */
  async function startTask(
    task: TaskRow,
    minutes: number,
    scope: string | null = task.next_action,
  ) {
    if (busy || (focus.running && focus.task_id === task.id)) return;
    setBusy(true);
    setConfirmingFinish(false);
    try {
      const result = await startFocus(
        task.id,
        runLabel(task.title, scope),
        minutes,
      );
      setFocus(result.focus);
      say(result.line);
      rememberTarget(window.sessionStorage, task.id, minutes);
      setSelectedId(null);
      setEditingScope(false);
      setPeeking(false); // a fresh start goes to the bar
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "action failed — try again");
    } finally {
      setBusy(false);
    }
  }

  /** the ▸ / start path: a task owed its scope opens the form instead
   * of running blind; everything else starts straight away */
  function startOrAsk(task: TaskRow, minutes: number) {
    if (needsScope(task)) {
      if (selectedId !== task.id) selectTask(task);
      return;
    }
    void startTask(task, minutes);
  }

  /** the form's "start": save the line, then run the clock with it. the
   * save is best-effort - a server hiccup must never keep a block from
   * starting; the label carries the scope regardless */
  async function saveScopeAndStart(task: TaskRow, minutes: number) {
    const scope = scopeDraft.trim().slice(0, SCOPE_CAP);
    if (!scope) {
      // an empty line is "just start" by another route
      justStart(task, minutes);
      return;
    }
    if (token) {
      try {
        await patchTask(token, task.id, { next_action: scope });
      } catch {
        // the clock still starts; the line lives in the run label
      }
    }
    await startTask(task, minutes, scope);
  }

  /** "just start": no scope today for this task - remembered locally */
  function justStart(task: TaskRow, minutes: number) {
    if (todayIso) rememberScopeSkip(window.localStorage, task.id, todayIso);
    void startTask(task, minutes, null);
  }

  /** click-to-edit on an existing scope (same PATCH; blank clears it) */
  async function saveScope(task: TaskRow) {
    if (!token) return;
    const scope = scopeDraft.trim().slice(0, SCOPE_CAP) || null;
    setBusy(true);
    try {
      await patchTask(token, task.id, { next_action: scope });
      setEditingScope(false);
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "couldn’t save that");
    } finally {
      setBusy(false);
    }
  }

  /** set aside (§2): a today decision. the running task pauses FIRST -
   * the patch route never touches the sidecar clock, so this window owns
   * the ordering (pause banks the run; an open question freezes it) */
  async function setAside(task: TaskRow) {
    if (!token || busy) return;
    if (focus.running && focus.task_id === task.id) {
      const stopped = await doPause();
      if (!stopped) {
        // a sidecar outage or a refused transition: the clock may still
        // be counting, so the task stays where it is (sol, #87)
        showNotice("couldn’t pause the timer — task unchanged");
        return;
      }
    }
    setBusy(true);
    try {
      await patchTask(token, task.id, { set_aside: true });
      setSelectedId(null);
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "couldn’t set that aside");
    } finally {
      setBusy(false);
    }
  }

  /** the parked row's three doors (§2's table) */
  async function parkedAction(
    task: TaskRow,
    action: "tomorrow" | "bring_back" | "let_go",
  ) {
    if (!token || busy || !todayIso) return;
    const patch =
      action === "tomorrow"
        ? {
            scheduled: tomorrowOf(todayIso),
            set_aside: false,
            // "in motion" would otherwise keep showing it tomorrow
            ...(task.status === "in_progress" ? { status: "todo" } : {}),
          }
        : action === "bring_back"
          ? { set_aside: false }
          : { status: "deprioritized", set_aside: false };
    setBusy(true);
    try {
      await patchTask(token, task.id, patch);
      setAsideId(null);
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "action failed — try again");
    } finally {
      setBusy(false);
    }
  }

  /** mark the task done on the chordial server, with the offline ledger
   * as the fallback - the finish is real either way */
  async function markTaskDone(taskId: number | null | undefined) {
    if (token && typeof taskId === "number") {
      try {
        await setTaskStatus(token, taskId, "done");
      } catch {
        setPendingDone(addPendingDone(window.localStorage, taskId));
      }
    }
    refreshToday();
  }

  /** stop the clock. resolves true only when the sidecar confirms the
   * clock is no longer running - callers that act on "stopped" (set
   * aside) must not proceed on a swallowed error */
  async function doPause(resolution?: Resolution): Promise<boolean> {
    setBusy(true);
    setConfirmingFinish(false);
    setConfirmingPause(false);
    try {
      const result = await pauseFocus(resolution);
      setFocus(result.focus);
      if (result.offer !== undefined) setOffer(result.offer ?? null);
      say(result.line);
      return !result.focus.running;
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "action failed — try again");
      return false;
    } finally {
      setBusy(false);
    }
  }

  // only the ACTIVE run's question may ride a transition - a held run's
  // question resolves on the card, which also banks it (a transition
  // resolution against it would orphan the frozen run unbanked)
  const offerOnActiveRun =
    offer !== null && focus.running && offer.run_id === focus.run_id;

  async function onPause() {
    if (busy) return;
    // an open question rides the transition: the pause button becomes the
    // combined choice instead of quietly banking contested time
    if (offerOnActiveRun && !confirmingPause) {
      setConfirmingPause(true);
      setConfirmingFinish(false);
      return;
    }
    await doPause();
  }

  async function doFinish(resolution?: Resolution) {
    setBusy(true);
    setConfirmingFinish(false);
    setConfirmingPause(false);
    const taskId = focus.task_id;
    try {
      const result = await finishFocus(resolution);
      setFocus(result.focus);
      if (result.offer !== undefined) setOffer(result.offer ?? null);
      say(result.line);
      if (result.held) return; // frozen: the answer will finish it
      flourish();
      await markTaskDone(taskId);
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "action failed — try again");
    } finally {
      setBusy(false);
    }
  }

  async function onFinish() {
    if (busy) return;
    // an open question or an early landing deserves one gentle "you sure?"
    if ((offerOnActiveRun || runSeconds < targetSeconds) && !confirmingFinish) {
      setConfirmingFinish(true);
      setConfirmingPause(false);
      return;
    }
    await doFinish();
  }

  /** the bar's pause: a combined choice (open question) needs the den */
  async function onBarPause() {
    if (busy) return;
    if (offerOnActiveRun) {
      setPeeking(true);
      setConfirmingPause(true);
      setConfirmingFinish(false);
      return;
    }
    await doPause();
  }

  async function onBarFinish() {
    if (busy) return;
    if (offerOnActiveRun) {
      setPeeking(true);
      setConfirmingFinish(true);
      setConfirmingPause(false);
      return;
    }
    await onFinish();
  }

  /** answer the card in place: remove a candidate boundary or keep all
   * time. a frozen run banks on the answer, wearing its transition. */
  async function doResolve(action: "remove" | "keep", at?: string) {
    if (!offer || busy) return;
    setBusy(true);
    const resolved = offer;
    try {
      const result = await resolveRewind(resolved.offer_uuid, action, at);
      setFocus(result.focus);
      // the next authoritative question, straight from the response -
      // another held block's card must not be lost to a broadcast race
      setOffer(result.offer ?? null);
      say(result.line);
      if (action === "remove" && !result.run) {
        // in-run apply: show the resting state with its undo
        const boundary = at ?? resolved.candidates[0]?.at;
        const chosen = resolved.candidates.find((c) => c.at === boundary);
        setApplied({
          offerUuid: resolved.offer_uuid,
          removed: chosen?.removed_seconds ?? 0,
          credited: result.focus.run_seconds ?? 0,
        });
        if (appliedTimer.current !== null) clearTimeout(appliedTimer.current);
        appliedTimer.current = setTimeout(
          () => setApplied(null),
          APPLIED_LINGER_MS,
        );
      }
      if (result.run?.reason === "finished") {
        flourish();
        await markTaskDone(resolved.task_id);
      }
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "action failed — try again");
    } finally {
      setBusy(false);
    }
  }

  async function doUndo() {
    if (!applied || busy) return;
    setBusy(true);
    try {
      const result = await undoRewind(applied.offerUuid);
      setFocus(result.focus);
      setOffer(result.offer ?? null);
      setApplied(null);
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "action failed — try again");
    } finally {
      setBusy(false);
    }
  }

  async function onAddTask(e: React.FormEvent) {
    // adding never touches the running clock - jot it, keep working,
    // switch when YOU choose
    e.preventDefault();
    const title = newTitle.trim();
    if (!title || !token || busy) return;
    setBusy(true);
    try {
      await createTask(token, title);
      setNewTitle("");
    } catch (err) {
      showNotice(err instanceof Error ? err.message : "couldn’t add that");
    } finally {
      setBusy(false);
    }
  }

  // --- the window's own controls (§11.2): hide, never quit --------------

  function onHide() {
    hideWindow().catch(() => {});
  }

  function onToggleTop() {
    const next = onTop === false;
    setAlwaysOnTop(next)
      .then(() => setOnTop(next))
      .catch(() => {});
  }

  function onAutoBarChange(on: boolean) {
    setAutoBar(window.localStorage, on);
    setAutoBarState(on);
  }

  const windowControls = (compact: boolean) => (
    <div className={`deer-win${compact ? " compact" : ""}`}>
      <button
        className={`deer-win-btn${onTop === false ? " off" : ""}`}
        onClick={onToggleTop}
        aria-pressed={onTop !== false}
        title={
          onTop === false
            ? "keep on top"
            : "unpin window"
        }
      >
        📌
      </button>
      <button
        className="deer-win-btn"
        onClick={onHide}
        aria-label="minimize"
        title="hide — reopen from chordial’s sidebar or the tray"
      >
        –
      </button>
      <button
        className="deer-win-btn"
        onClick={onHide}
        aria-label="close"
        title="hide companion — reopen from chordial’s sidebar; the clock keeps counting"
      >
        ×
      </button>
    </div>
  );

  // the bar's one text slot: a notice first, then a saying while fresh
  const barLine =
    notice ??
    (saying && now - saying.seq < BAR_LINE_MS ? saying.text : null);

  // --- the bar: the slim form while a clock runs (§11.1) ----------------

  if (form === "bar") {
    return (
      <>
      {leaves}
      <div className="deer-bar" data-tauri-drag-region="true">
        <button
          className={`deer-bar-deer${overtime ? " perked" : ""}${
            activity?.blocked ? " hushed" : ""
          }`}
          onClick={() => setPeeking(true)}
          title="show tasks — timer keeps running"
          aria-label="show tasks"
        >
          🦌
        </button>
        <div className="deer-bar-text" data-tauri-drag-region="true">
          {barLine ? (
            <span className={`deer-bar-line${notice ? " notice" : ""}`}>
              <InlineContent content={barLine} />
            </span>
          ) : (
            <span className="deer-bar-title" data-tauri-drag-region="true">
              {activity?.blocked ? (
                "notifications paused"
              ) : runningLabel ? (
                <>
                  {runningLabel.title}
                  {runningLabel.scope && (
                    <span className="deer-bar-scope">
                      {" · "}
                      {runningLabel.scope}
                    </span>
                  )}
                </>
              ) : (
                "timer running"
              )}
            </span>
          )}
          <span className="deer-bar-fill">
            <span
              className="deer-bar-fill-bar"
              style={{
                width: `${Math.min(1, runSeconds / targetSeconds) * 100}%`,
              }}
            />
          </span>
        </div>
        <span className={`deer-bar-clock${overtime ? " over" : ""}`}>
          {mmss(runSeconds)}
        </span>
        <div className="deer-bar-controls">
          {offer && (
            <button
              className="deer-bar-chip"
              onClick={() => {
                setPeeking(true);
                expandCard();
              }}
              title="review time adjustment"
              aria-label="open the question"
            >
              ?
            </button>
          )}
          {confirmingFinish ? (
            <>
              <button
                className="deer-bar-btn finish confirm"
                onClick={onFinish}
                disabled={busy}
                title="finish early?"
              >
                early ✓
              </button>
              <button
                className="deer-bar-btn"
                onClick={() => setConfirmingFinish(false)}
                title="keep going"
                aria-label="keep going"
              >
                ✕
              </button>
            </>
          ) : (
            <>
              <button
                className="deer-bar-btn"
                onClick={onBarPause}
                disabled={busy}
                title="pause — the minutes bank"
                aria-label="pause"
              >
                ❚❚
              </button>
              <button
                className="deer-bar-btn finish"
                onClick={onBarFinish}
                disabled={busy}
                title="finished"
                aria-label="finished"
              >
                ✓
              </button>
            </>
          )}
        </div>
        {windowControls(true)}
      </div>
      </>
    );
  }

  // --- the den ---------------------------------------------------------

  // the whole window is a handle: the deer is undecorated, so dragging
  // her anywhere that isn't a control (the gaps, the deer herself) moves
  // the window. tauri only honours the attribute on the element under
  // the pointer, so it sits on the root, the strip, AND the deer - a
  // button or chip inside keeps its click. needs
  // core:window:allow-start-dragging in capabilities/default.json (the
  // core:default set doesn't include it - found on the first boxed run,
  // when she couldn't be moved at all).
  return (
    <>
    {leaves}
    <div className="deer-window" data-tauri-drag-region="true">
      <div className="deer-drag" data-tauri-drag-region="true">
        <span className="deer-caption" data-tauri-drag-region="true">companion</span>
        <span
          className={`deer-link-dot${connected ? " on" : ""}`}
          title={connected ? "timer connected" : "timer disconnected"}
        />
        {windowControls(false)}
      </div>

      <div className="deer-head" data-tauri-drag-region="true">
        <div
          className={`deer-self${focus.running ? " watching" : " loafing"}${
            overtime ? " perked" : ""
          }${activity?.blocked ? " hushed" : ""}`}
          aria-hidden="true"
          data-tauri-drag-region="true"
        >
          🦌
        </div>
        <div className="deer-bubble-slot" role="status" aria-live="polite">
          {saying && (
            <div className="deer-bubble">
              <span className="deer-bubble-text">
                <InlineContent content={saying.text} />
              </span>
              <button
                className="deer-bubble-dismiss"
                onClick={dismiss}
                aria-label="dismiss"
                title="dismiss"
              >
                ×
              </button>
            </div>
          )}
        </div>
      </div>

      {offer &&
        (cardExpanded || offer.frozen ? (
          <div className="deer-offer">
            <p className="deer-offer-line">
              {offer.frozen
                ? `clock's stopped — answering will ${frozenVerb(offer.frozen_reason)}. `
                : ""}
              {quietLine(offer)}
            </p>
            <div className="deer-offer-actions">
              {offer.candidates.length > 0 && (
                <button
                  className="deer-offer-remove"
                  onClick={() => doResolve("remove")}
                  disabled={busy}
                >
                  {removeLabel(
                    offer.candidates[0].removed_seconds,
                    // live for an active offer: the clock keeps running
                    // while the question is deferred, and the promise
                    // must match what applying actually produces
                    offerOnActiveRun
                      ? Math.max(
                          0,
                          runSeconds - offer.candidates[0].removed_seconds,
                        )
                      : offer.candidates[0].credited_seconds,
                  )}
                </button>
              )}
              <button
                className="deer-offer-keep"
                onClick={() => doResolve("keep")}
                disabled={busy}
              >
                keep all time
              </button>
            </div>
            {offer.candidates.length > 1 &&
              (showAlt ? (
                <button
                  className="deer-offer-alt"
                  onClick={() => doResolve("remove", offer.candidates[1].at)}
                  disabled={busy}
                >
                  {altLabel(offer.candidates[1])}
                </button>
              ) : (
                <button
                  className="deer-offer-alt subtle"
                  onClick={() => setShowAlt(true)}
                >
                  more options
                </button>
              ))}
          </div>
        ) : (
          <button className="deer-offer-chip" onClick={expandCard}>
            {chipLabel(offer)}
          </button>
        ))}

      {applied && !offer && (
        <div className="deer-undo">
          <span>{appliedLabel(applied.removed, applied.credited)}</span>
          <button onClick={doUndo} disabled={busy}>
            undo
          </button>
        </div>
      )}

      {focus.running && (
        <div className="deer-session">
          {runningLabel && (
            <p className="deer-label" title={focus.label ?? undefined}>
              {runningLabel.title}
              {runningLabel.scope && (
                <span className="deer-label-scope">{runningLabel.scope}</span>
              )}
            </p>
          )}
          <p className={`deer-clock${overtime ? " over" : ""}`}>
            {mmss(runSeconds)}
            {overtime && (
              <span className="deer-extra">
                +{mmss(runSeconds - targetSeconds)} extra
              </span>
            )}
          </p>
          <div className="deer-fill">
            <div
              className="deer-fill-bar"
              style={{
                width: `${Math.min(1, runSeconds / targetSeconds) * 100}%`,
              }}
            />
          </div>
          <div className="deer-controls">
            {confirmingPause && offerOnActiveRun && offer ? (
              <>
                <button
                  className="deer-pause"
                  onClick={() =>
                    doPause({
                      offer_uuid: offer.offer_uuid,
                      action: "remove",
                    })
                  }
                  disabled={busy}
                >
                  remove {amountLabel(offer.contested_seconds)} & pause
                </button>
                <button
                  className="deer-pause"
                  onClick={() =>
                    doPause({ offer_uuid: offer.offer_uuid, action: "keep" })
                  }
                  disabled={busy}
                >
                  keep all & pause
                </button>
                <button
                  className="deer-cancel"
                  onClick={() => setConfirmingPause(false)}
                >
                  keep going
                </button>
              </>
            ) : confirmingFinish && offerOnActiveRun && offer ? (
              <>
                <button
                  className="deer-finish confirm"
                  onClick={() =>
                    doFinish({
                      offer_uuid: offer.offer_uuid,
                      action: "remove",
                    })
                  }
                  disabled={busy}
                >
                  remove {amountLabel(offer.contested_seconds)} & finish
                </button>
                <button
                  className="deer-finish confirm"
                  onClick={() =>
                    doFinish({ offer_uuid: offer.offer_uuid, action: "keep" })
                  }
                  disabled={busy}
                >
                  keep all & finish
                </button>
                <button
                  className="deer-cancel"
                  onClick={() => setConfirmingFinish(false)}
                >
                  keep going
                </button>
              </>
            ) : (
              <>
                <button
                  className="deer-pause"
                  onClick={onPause}
                  disabled={busy}
                >
                  pause
                </button>
                {confirmingFinish ? (
                  <>
                    <button
                      className="deer-finish confirm"
                      onClick={onFinish}
                      disabled={busy}
                    >
                      finish early?
                    </button>
                    <button
                      className="deer-cancel"
                      onClick={() => setConfirmingFinish(false)}
                    >
                      keep going
                    </button>
                  </>
                ) : (
                  <button
                    className="deer-finish"
                    onClick={onFinish}
                    disabled={busy}
                  >
                    finished ✓
                  </button>
                )}
              </>
            )}
          </div>
          {autoBar && peeking && (
            <button
              className="deer-to-bar"
              onClick={() => setPeeking(false)}
              title="show timer"
            >
              show timer
            </button>
          )}
        </div>
      )}

      {token ? (
        <div className="deer-tasks">
          <TaskSyncStatus
            error={taskError}
            notice={notice}
            updatedAt={updatedAt}
            onRefresh={refreshToday}
            summary={
              today
                ? dayStatus({
                    blocked: !!activity?.blocked,
                    drifting: !!activity?.drifting,
                    running: focus.running,
                    overtime,
                    openCount: openTasks.length,
                    doneCount: doneTasks.length,
                    minutesToday,
                  })
                : undefined
            }
          />
          <ul>
            {openTasks.map((task) => {
              const active = focus.running && focus.task_id === task.id;
              const selected = selectedId === task.id;
              if (pendingDone.includes(task.id)) {
                // finished here, not yet confirmed by the server: visibly
                // done (never open-and-clickable), honestly still syncing
                return (
                  <li key={task.id}>
                    <div className="deer-task done syncing">
                      <span className="deer-task-check" aria-hidden="true">
                        ✓
                      </span>
                      <span className="deer-task-title">{task.title}</span>
                      <span className="deer-task-note">syncing…</span>
                    </div>
                  </li>
                );
              }
              return (
                <li key={task.id}>
                  <div
                    className={`deer-task${active ? " active" : ""}${
                      selected ? " selected" : ""
                    }`}
                    role="button"
                    tabIndex={0}
                    aria-label={task.title}
                    aria-expanded={selected}
                    onClick={() => selectTask(task)}
                    onKeyDown={(e) => {
                      // escape closes the row from anywhere inside it -
                      // a chip or the start button included
                      if (e.key === "Escape") {
                        e.preventDefault();
                        setSelectedId(null);
                        e.currentTarget.focus();
                        return;
                      }
                      // the row's own keys only - an inner button's Enter
                      // already clicked it and must not start twice
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        if (selected && !active) {
                          startOrAsk(task, chipMinutes ?? pomMinutes);
                        } else {
                          selectTask(task);
                        }
                      }
                    }}
                  >
                    <div className="deer-task-head">
                      <span className="deer-task-title" title={task.title}>
                        {task.title}
                        {task.next_action && !selected && (
                          <span className="deer-task-scope">
                            {task.next_action}
                          </span>
                        )}
                      </span>
                      {active ? (
                        <span className="deer-task-live">running</span>
                      ) : (
                        <button
                          className="deer-task-play"
                          disabled={busy}
                          title={
                            focus.running
                              ? "switch to this one"
                              : "start this one"
                          }
                          aria-label={
                            focus.running
                              ? `switch to ${task.title}`
                              : `start ${task.title}`
                          }
                          onClick={(e) => {
                            e.stopPropagation();
                            startOrAsk(
                              task,
                              lastTarget(
                                window.sessionStorage,
                                task.id,
                                pomMinutes,
                              ),
                            );
                          }}
                        >
                          ▸
                        </button>
                      )}
                    </div>
                    <span className="deer-task-bar">
                      <span
                        className="deer-task-fill"
                        style={{ width: `${taskFill(task) * 100}%` }}
                      />
                    </span>
                    {selected && (
                      <div
                        className="deer-task-detail"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {active ? (
                          <>
                            <span className="deer-task-note">
                              this clock is running — pause or finish above
                            </span>
                            <button
                              className="deer-aside-btn"
                              disabled={busy || !token}
                              onClick={() => setAside(task)}
                            >
                              {SCOPE_COPY.setAsideRunning}
                            </button>
                          </>
                        ) : (
                          <>
                            {needsScope(task) || editingScope ? (
                              // the scoping form (§3): one line, a target,
                              // start - or just start, remembered for today
                              <div className="deer-scope-form">
                                <label className="deer-scope-prompt">
                                  {SCOPE_COPY.prompt}
                                  <input
                                    className="deer-scope-input"
                                    autoFocus
                                    value={scopeDraft}
                                    maxLength={SCOPE_CAP}
                                    placeholder={SCOPE_COPY.placeholder}
                                    onChange={(e) =>
                                      setScopeDraft(e.currentTarget.value)
                                    }
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") {
                                        e.preventDefault();
                                        if (editingScope) void saveScope(task);
                                        else
                                          void saveScopeAndStart(
                                            task,
                                            chipMinutes ?? pomMinutes,
                                          );
                                      }
                                    }}
                                  />
                                </label>
                              </div>
                            ) : (
                              task.next_action && (
                                <button
                                  className="deer-scope-line"
                                  title={SCOPE_COPY.editScope}
                                  onClick={() => {
                                    setScopeDraft(task.next_action ?? "");
                                    setEditingScope(true);
                                  }}
                                >
                                  ↳ {task.next_action}
                                </button>
                              )
                            )}
                            <div
                              className="deer-chips"
                              role="radiogroup"
                              aria-label="block target"
                            >
                              {targetChoices(pomMinutes).map((m) => (
                                <button
                                  key={m}
                                  className={`deer-chip${
                                    chipMinutes === m ? " on" : ""
                                  }`}
                                  role="radio"
                                  aria-checked={chipMinutes === m}
                                  onClick={() => setChipMinutes(m)}
                                >
                                  {m}m
                                </button>
                              ))}
                            </div>
                            {editingScope ? (
                              <>
                                <button
                                  className="deer-start"
                                  disabled={busy}
                                  onClick={() => saveScope(task)}
                                >
                                  save
                                </button>
                                <button
                                  className="deer-just-start"
                                  onClick={() => {
                                    setEditingScope(false);
                                    setScopeDraft(task.next_action ?? "");
                                  }}
                                >
                                  never mind
                                </button>
                              </>
                            ) : needsScope(task) ? (
                              <>
                                <button
                                  className="deer-start"
                                  disabled={busy}
                                  onClick={() =>
                                    saveScopeAndStart(
                                      task,
                                      chipMinutes ?? pomMinutes,
                                    )
                                  }
                                >
                                  {focus.running
                                    ? "switch to this"
                                    : SCOPE_COPY.start}
                                </button>
                                <button
                                  className="deer-just-start"
                                  disabled={busy}
                                  onClick={() =>
                                    justStart(task, chipMinutes ?? pomMinutes)
                                  }
                                >
                                  {SCOPE_COPY.justStart}
                                </button>
                              </>
                            ) : (
                              <button
                                className="deer-start"
                                disabled={busy}
                                onClick={() =>
                                  startTask(task, chipMinutes ?? pomMinutes)
                                }
                              >
                                {focus.running ? "switch to this" : "start"}
                              </button>
                            )}
                            <button
                              className="deer-aside-btn"
                              disabled={busy || !token}
                              onClick={() => setAside(task)}
                            >
                              {SCOPE_COPY.setAside}
                            </button>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
            {doneTasks.map((task) => (
              <li key={`done-${task.id}`}>
                <div className="deer-task done">
                  <span className="deer-task-check" aria-hidden="true">
                    ✓
                  </span>
                  <span className="deer-task-title">{task.title}</span>
                  <span className="deer-task-bar">
                    <span
                      className="deer-task-fill done"
                      style={{ width: "100%" }}
                    />
                  </span>
                </div>
              </li>
            ))}
            {asideTasks.map((task) => {
              // parked for today (§2): greyed, below the wins, still a
              // door - tomorrow / bring back / let it go
              const open = asideId === task.id;
              return (
                <li key={`aside-${task.id}`}>
                  <div
                    className={`deer-task aside${open ? " selected" : ""}`}
                    role="button"
                    tabIndex={0}
                    aria-label={`${task.title}, set aside`}
                    aria-expanded={open}
                    onClick={() => {
                      setAsideId(open ? null : task.id);
                      setSelectedId(null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") {
                        e.preventDefault();
                        setAsideId(null);
                        e.currentTarget.focus();
                        return;
                      }
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setAsideId(open ? null : task.id);
                      }
                    }}
                  >
                    <div className="deer-task-head">
                      <span className="deer-task-title" title={task.title}>
                        {task.title}
                      </span>
                      <span className="deer-task-note">
                        {SCOPE_COPY.asideHeading}
                      </span>
                    </div>
                    {open && (
                      <div
                        className="deer-task-detail deer-aside-actions"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <span className="deer-task-note">
                          {SCOPE_COPY.asideNote}
                        </span>
                        <div className="deer-aside-doors">
                          <button
                            className="deer-aside-door"
                            disabled={busy}
                            onClick={() => parkedAction(task, "tomorrow")}
                          >
                            {SCOPE_COPY.tomorrow}
                          </button>
                          <button
                            className="deer-aside-door"
                            disabled={busy}
                            onClick={() => parkedAction(task, "bring_back")}
                          >
                            {SCOPE_COPY.bringBack}
                          </button>
                          <button
                            className="deer-aside-door letgo"
                            disabled={busy}
                            onClick={() => parkedAction(task, "let_go")}
                          >
                            {SCOPE_COPY.letGo}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
            {today && openTasks.length === 0 &&
              doneTasks.length === 0 &&
              asideTasks.length === 0 && (
                <li className="deer-empty">
                  no tasks scheduled.
                </li>
              )}
          </ul>
          <form className="deer-add" onSubmit={onAddTask}>
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.currentTarget.value)}
              placeholder="add a task…"
              aria-label="New task title"
              maxLength={300}
            />
            <button type="submit" aria-label="Add task" disabled={busy || !newTitle.trim()}>
              +
            </button>
          </form>
        </div>
      ) : (
        <p className="deer-unlinked">
          link your device in the main window.
        </p>
      )}

      <label className="deer-pref">
        <input
          type="checkbox"
          checked={autoBar}
          onChange={(e) => onAutoBarChange(e.currentTarget.checked)}
        />
        slim bar while a clock runs
      </label>
    </div>
    </>
  );
}

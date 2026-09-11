"""stuck mode: the episode, the brief, the validator, the fallback ladder
(docs/STUCK_MODE_DESIGN.md sections 5-7).

one press of "i'm stuck" opens an episode. a pip turn reads the brief
(section 5.2) and writes three proposals of three kinds through the
`propose_unstuck` tool; the server validates and bounds them (section 5.3),
assigns their ids, and only then does the card exist. when the turn is out
of reach - no orchestrator, a timeout, a refused tool call - the fallback
ladder (section 6) becomes the card, deterministically and model-free.

nothing here mutates the workspace except `react(..., "accepted")`, which
writes the ONE prepared next_action in the same transaction that claims the
proposal. generating and browsing proposals changes nothing outside the two
stuck tables; the rest branch changes nothing at all.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from config import Config
from src.database.database import get_db
from src.database.models import StuckEpisode, StuckReaction, Task
from src.services.scorecard import _overlap
from src.services.workspace import vocab
from src.utils.timezone_utils import is_within_quiet_hours, utc_now

logger = logging.getLogger(__name__)

# --- vocabulary (section 3 + 5.3) --------------------------------------------

KINDS = ("step", "switch", "thread", "body", "sound", "rest", "company")

# every action must match its kind (5.3)
ACTIONS_FOR_KIND: dict[str, frozenset] = {
    "step": frozenset({"start_task"}),
    "switch": frozenset({"start_task"}),
    "thread": frozenset({"start_task"}),
    "body": frozenset({"pause_and_away", "body_here"}),
    "sound": frozenset({"point_to_sound"}),
    "rest": frozenset({"rest_here"}),
    "company": frozenset({"start_company"}),
}
ACTIONS = frozenset(a for s in ACTIONS_FOR_KIND.values() for a in s)
MINUTES_ALLOWED = (2, 5, 8, 10)
DEFAULT_MINUTES = {"step": 2, "switch": 2, "thread": 2, "body": 8,
                   "company": 5}
WHY_REGISTERS = ("matters", "soft")
LINE_CAP = 140
ENOUGH_CAP = 100
WHY_CAP = 120
NEXT_ACTION_CAP = 140
PROPOSALS_PER_GENERATION = 3
# the third "something different" earns one more turn; after that the
# client shows the rest branch as the honest end of the ladder (2.3)
MAX_GENERATIONS = 2

# server-issued evidence ids the model may cite (5.3); the validator checks
# each against the snapshot, so a proposal can never invent its evidence
CODE_LATE_LAST_NIGHT = "late_last_night"
CODE_PAST_QUIET_HOURS = "past_quiet_hours"
CODE_HOURS_SINCE_LAST_RUN = "hours_since_last_run"
CODE_NO_RUNS_TODAY = "no_runs_today"
CODE_NO_SUITABLE_TASK = "no_suitable_task"
CODE_CLOCK_RUNNING = "clock_running"

LATE_NIGHT_FLOOR_SECONDS = 30 * 60      # half an hour past 23:00 counts
HOURS_SINCE_RUN_FLOOR = 3               # the body-first threshold (section 6)

# episode states (5.1)
THINKING = "thinking"
READY = "ready"
FALLBACK = "fallback"
FAILED = "failed"
ACCEPTED = "accepted"
RESTED = "rested"
CLOSED = "closed"
TERMINAL = frozenset({ACCEPTED, RESTED, CLOSED, FAILED})
CARD_SHOWING = frozenset({READY, FALLBACK})
REACTIONS = ("accepted", "different", "too_much", "closed")
SURFACES = ("companion", "home", "tray", "room", "telegram")


# --- authored copy: the fallback ladder's words (section 6) -----------------
# one place, so a voice pass replaces strings without touching the ladder.

FALLBACK_COPY = {
    "step_flagged_enough": "enough = you did that one piece, however roughly",
    "step_open_line": 'open "{title}" and read what\'s there. two minutes.',
    "step_open_enough": "enough = you know what it's about again",
    "company_line": "sit here for five minutes. you do not have to choose anything yet.",
    "company_enough": "enough = five minutes passed with company",
    "body_out_line": "a glass of water, then eight minutes outside. no phone.",
    "body_out_enough": "enough = you came back",
    "body_here_line": "stand up, stretch, sit back down.",
    "body_here_enough": "enough = you stood up",
    "rest_sleep_line": "sleep is the move. the list will be exactly where you left it.",
    "rest_sleep_enough": "enough = you closed this",
    "rest_here_line": "close the list for now. nothing on it changes.",
    "rest_here_enough": "enough = you closed this",
    "thread_line": 'reopen "{label}" exactly where you stopped. just look.',
    "thread_enough": "enough = you can see where you were",
    "switch_line": 'a different thing: open "{title}" instead. two minutes.',
    "switch_enough": "enough = you know what it's about again",
    "sound_line": "put on something without words - rain, or a playlist you already know.",
    "sound_enough": "enough = it's playing",
}


# --- evidence: what the brief and the validator both read (5.2) -------------

@dataclass
class Evidence:
    """one read of the person's situation, shared by the brief the model
    sees and the validator that checks what it wrote."""
    now_local: datetime
    in_quiet: bool = False
    late_last_night_seconds: int = 0
    minutes_since_last_run: Optional[int] = None
    runs_today: int = 0
    banked_minutes: int = 0
    clock: str = "idle"                  # running | paused | idle
    clock_label: Optional[str] = None
    tasks: list = field(default_factory=list)   # dicts, see _task_row
    plans: dict = field(default_factory=dict)   # plan_id -> {title, why}
    recent: list = field(default_factory=list)  # recent episode summaries
    last_run_task_id: Optional[int] = None      # for the thread kind
    last_run_label: Optional[str] = None
    focus_task_id: Optional[int] = None         # pressed from a task's row

    @property
    def task_ids(self) -> set:
        return {t["id"] for t in self.tasks}

    @property
    def plan_ids(self) -> set:
        return set(self.plans)

    @property
    def codes(self) -> set:
        codes = set()
        if self.late_last_night_seconds >= LATE_NIGHT_FLOOR_SECONDS:
            codes.add(CODE_LATE_LAST_NIGHT)
        if self.in_quiet:
            codes.add(CODE_PAST_QUIET_HOURS)
        if (self.minutes_since_last_run is not None
                and self.minutes_since_last_run >= HOURS_SINCE_RUN_FLOOR * 60):
            codes.add(CODE_HOURS_SINCE_LAST_RUN)
        if self.runs_today == 0 and self.clock == "idle":
            codes.add(CODE_NO_RUNS_TODAY)
        if not self.tasks:
            codes.add(CODE_NO_SUITABLE_TASK)
        if self.clock == "running":
            codes.add(CODE_CLOCK_RUNNING)
        return codes


def gather(user_uuid: str, today=None, recent: Iterable[dict] = (),
           focus_task_id: Optional[int] = None) -> Evidence:
    """the evidence, from the day snapshot (dogfood section 5.1) plus the
    workspace. `today` is an already-computed FocusDay when the caller has
    one (the enrich path reads exactly one snapshot); otherwise read it
    here. db reads only; never writes."""
    from src.services import focus_day
    from src.services.workspace import get_store

    fd = today if today is not None else focus_day.snapshot(user_uuid)
    ev = Evidence(now_local=fd.now)
    ev.in_quiet = is_within_quiet_hours(
        fd.now.hour, Config.QUIET_HOURS_START, Config.QUIET_HOURS_END)
    ev.runs_today = fd.run_count
    ev.banked_minutes = fd.banked_seconds // 60
    ev.minutes_since_last_run = fd.idle_minutes
    if fd.running is not None:
        ev.clock, ev.clock_label = "running", fd.running.label
    elif fd.frozen is not None:
        ev.clock, ev.clock_label = "paused", fd.frozen.label
    ev.late_last_night_seconds = _late_seconds_around(fd, user_uuid)
    if fd.last_run is not None:
        ev.last_run_task_id = fd.last_run.task_id
        ev.last_run_label = fd.last_run.label

    store = get_store()
    by_id = {t["id"]: t for t in store.list_tasks(user_uuid)}
    today_iso = fd.day.isoformat()
    for p in fd.planned:
        t = by_id.get(p["id"])
        if t is None or t.get("set_aside_on") == today_iso:
            continue    # parked today is off the list (dogfood section 2)
        ev.tasks.append(_task_row(t, p, fd.flags.get(p["id"], ())))
    for plan_id in {t["plan_id"] for t in ev.tasks if t.get("plan_id")}:
        try:
            plan = store.get_plan(user_uuid, plan_id)
        except Exception:
            continue
        ev.plans[plan_id] = {"title": plan.get("title"), "why": plan.get("why")}
    ev.recent = list(recent)
    # the row the button was pressed from, when it's still on the list
    if focus_task_id is not None and focus_task_id in ev.task_ids:
        ev.focus_task_id = focus_task_id
    return ev


def _task_row(t: dict, planned: dict, signals) -> dict:
    return {
        "id": t["id"], "title": t["title"], "priority": t.get("priority"),
        "scheduled": t.get("scheduled"), "pom_estimate": t.get("pom_estimate"),
        "next_action": t.get("next_action"),
        "banked_seconds": planned.get("banked_seconds") or 0,
        "reschedules": t.get("reschedules") or 0,
        "set_aside_count": t.get("set_aside_count") or 0,
        "plan_id": t.get("plan_id"), "plan_title": t.get("plan_title"),
        "signals": list(signals),
    }


LATE_WINDOW_START = time(23, 0)
LATE_WINDOW_END = time(6, 0)


def _late_seconds_around(fd, user_uuid: str) -> int:
    """seconds of work inside LAST NIGHT only - the single window from
    yesterday 23:00 to today 06:00 local. a 2am run the night before last
    is not last night (sol, #89). guarded: yesterday's read failing costs
    the code, never the episode."""
    from src.services import focus_day
    window_start = datetime.combine(fd.day - timedelta(days=1), LATE_WINDOW_START)
    window_end = datetime.combine(fd.day, LATE_WINDOW_END)
    runs = list(fd.runs)
    try:
        runs += list(focus_day.snapshot(user_uuid, fd.day - timedelta(days=1)).runs)
    except Exception:
        logger.exception("yesterday's snapshot failed for %s", user_uuid)
    total = 0
    for r in runs:
        end = r.ended_at
        start = end - timedelta(seconds=max(0, int(r.seconds or 0)))
        total += _overlap(start, end, window_start, window_end)
    return total


# --- the brief (5.2): the ambient block the turn reads ----------------------

def render_brief(ev: Evidence, surface: str = "companion",
                 rejected_kinds: Iterable[str] = ()) -> str:
    lines = [f'they pressed "i\'m stuck" from the {surface}. this is the whole '
             f"message: their capacity to direct themselves is low and they "
             f"are asking you to take the initiative."]
    when = ev.now_local.strftime("%-I:%M%p").lower() + ev.now_local.strftime(", %A")
    if ev.clock == "running":
        clock = f'a clock is running on "{ev.clock_label}"'
    elif ev.clock == "paused":
        clock = f'a clock is paused on "{ev.clock_label}"'
    elif ev.minutes_since_last_run is not None:
        clock = f"clock idle; last run ended {ev.minutes_since_last_run} min ago"
    else:
        clock = "clock idle; no runs yet today"
    day = (f"banked {ev.banked_minutes} min in {ev.runs_today} runs today"
           if ev.runs_today else "nothing banked today")
    lines.append(f"right now: {when}. {clock}. {day}.")
    if CODE_LATE_LAST_NIGHT in ev.codes:
        mins = ev.late_last_night_seconds // 60
        lines.append(f"last night ran late: {mins} min of work past 11pm "
                     f"[evidence id: {CODE_LATE_LAST_NIGHT}]")
    if ev.in_quiet:
        lines.append(f"it is past their quiet hours [evidence id: {CODE_PAST_QUIET_HOURS}]")
    if CODE_HOURS_SINCE_LAST_RUN in ev.codes:
        lines.append(f"hours since anything ran [evidence id: {CODE_HOURS_SINCE_LAST_RUN}]")
    if CODE_NO_RUNS_TODAY in ev.codes:
        lines.append(f"no runs today [evidence id: {CODE_NO_RUNS_TODAY}]")
    if ev.focus_task_id is not None:
        focus = next((t for t in ev.tasks if t["id"] == ev.focus_task_id), None)
        if focus is not None:
            lines.append(f'they pressed it from the row of #{focus["id"]} '
                         f'"{focus["title"]}" - that is the task they mean; '
                         f"start there unless the evidence says otherwise.")
    if ev.tasks:
        lines.append("on today's list (task ids are the ONLY ids you may use):")
        for t in ev.tasks:
            bits = [f'#{t["id"]} "{t["title"]}"']
            if t.get("priority"):
                bits.append(f"({t['priority']})")
            bits.append(f'first piece: "{t["next_action"]}"' if t.get("next_action")
                        else "no first piece named yet")
            banked = t["banked_seconds"] // 60
            bits.append(f"{banked} min today" if banked else "untouched today")
            if t.get("signals"):
                bits.append("; ".join(t["signals"]))
            if t.get("plan_id") and t["plan_id"] in ev.plans:
                plan = ev.plans[t["plan_id"]]
                bits.append(f'plan #{t["plan_id"]} "{plan.get("title")}"')
                if plan.get("why"):
                    bits.append(f'their why, in their words: "{plan["why"]}"')
            lines.append("- " + " - ".join(bits))
    else:
        lines.append(f"nothing on today's list [evidence id: {CODE_NO_SUITABLE_TASK}] - "
                     f"no step or switch is possible; company, body, rest, sound, thread only.")
    if ev.recent:
        lines.append("recent stuck episodes, newest first:")
        for r in ev.recent:
            lines.append("- " + r)
    rejected = [k for k in rejected_kinds if k]
    if rejected:
        lines.append("already turned down this time (do not offer these kinds again): "
                     + ", ".join(rejected))
    return "\n".join(lines)


def instruction(who: str, rejected_kinds: Iterable[str] = ()) -> str:
    """the turn's instruction (5.2 draft). instruction copy, not pip's
    voice; the card register is the deer's, quiet and concrete."""
    rejected = ", ".join(k for k in rejected_kinds if k)
    angle = (f"\nthey already turned down: {rejected}. different angle - "
             f"other kinds only." if rejected else "")
    return (
        f"call propose_unstuck exactly once with THREE proposals of three "
        f"DIFFERENT kinds, the first being the one you'd bet on for {who} "
        f"this exact moment. this turn produces no message: the tool call is "
        f"the whole reply. write nothing else.{angle}\n"
        f"each proposal is one concrete sentence a body can do (a verb, a "
        f"place, a thing - never 'work on', never 'think about'), with what "
        f"counts as enough: a boundary a tired person can recognise without "
        f"judging themselves. never ask a question. never offer a list to "
        f"choose from. never 'which one'. never shrink the same task twice - "
        f"change the kind.\n"
        f"kinds: step (one entry action on a listed task), switch (a "
        f"different listed task, your provisional choice, reason in one "
        f"clause), thread (restore the last context: what the last run was "
        f"on, the unfinished decision), body (water, food, light, eight "
        f"minutes outside, stretch - one, chosen by the evidence), sound "
        f"(music or ambient noise to put on), rest (close the list; sleep "
        f"when the evidence says so), company (just sit here five minutes).\n"
        f"actions must match kinds: step/switch/thread -> start_task with "
        f"thing.task_id and prepared_step {{task_id, next_action}} (the "
        f"action instruction the clock runs on, <= 140 chars, never draft "
        f"prose); body -> pause_and_away (leaving the desk) or body_here (in "
        f"the chair); sound -> point_to_sound; rest -> rest_here; company -> "
        f"start_company. the smallest contribution that has to come from "
        f"them is the one you ask for; everything before it, name in the "
        f"prepared step - but you execute nothing and change nothing.\n"
        f"the why is theirs: one clause from their own words, rewritten to "
        f"the size of the step (why_register 'matters'), or the softer "
        f"register ('soft': today's rough attempt doesn't have to carry the "
        f"whole thing) when set-asides or reschedules show that caring is "
        f"what's making it heavy. rationale_codes may only be the evidence "
        f"ids in the brief."
    )


# --- validation (5.3) --------------------------------------------------------

class ProposalError(ValueError):
    """what the tool tells the model when the call is refused."""


def _text(value, cap: int, name: str, required: bool = True) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ProposalError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ProposalError(f"{name} must be a string")
    value = " ".join(value.split())
    if len(value) > cap:
        raise ProposalError(f"{name} is over {cap} characters")
    return value


def validate(raw, ev: Evidence, rejected_kinds: Iterable[str] = ()) -> list[dict]:
    """three distinct kinds, actions matching kinds, ids only from the
    brief, text within caps, evidence codes only from the snapshot. raises
    ProposalError with the reason; returns normalized proposals WITHOUT
    ids (the store assigns those, the model never invents identifiers)."""
    if not isinstance(raw, list):
        raise ProposalError("proposals must be a list")
    if len(raw) != PROPOSALS_PER_GENERATION:
        raise ProposalError(f"exactly {PROPOSALS_PER_GENERATION} proposals, "
                            f"got {len(raw)}")
    rejected = {k for k in rejected_kinds if k}
    out, kinds = [], []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ProposalError(f"proposal {i} must be an object")
        kind = item.get("kind")
        if kind not in KINDS:
            raise ProposalError(f"proposal {i}: unknown kind {kind!r}")
        if kind in kinds:
            raise ProposalError(f"proposal {i}: kind {kind!r} repeats - "
                                f"three DIFFERENT kinds")
        if kind in rejected:
            raise ProposalError(f"proposal {i}: kind {kind!r} was already "
                                f"turned down this episode")
        kinds.append(kind)
        action = item.get("action")
        if action not in ACTIONS_FOR_KIND[kind]:
            raise ProposalError(
                f"proposal {i}: action {action!r} does not match kind "
                f"{kind!r} (allowed: {', '.join(sorted(ACTIONS_FOR_KIND[kind]))})")
        p = {
            "kind": kind, "action": action,
            "line": _text(item.get("line"), LINE_CAP, f"proposal {i} line"),
            "enough": _text(item.get("enough"), ENOUGH_CAP,
                            f"proposal {i} enough"),
            "why": _text(item.get("why"), WHY_CAP, f"proposal {i} why",
                         required=False),
            "why_register": item.get("why_register") or "matters",
            "thing": None, "prepared_step": None, "rationale_codes": [],
        }
        if p["why_register"] not in WHY_REGISTERS:
            raise ProposalError(f"proposal {i}: why_register must be one of "
                                f"{', '.join(WHY_REGISTERS)}")
        minutes = item.get("minutes")
        if minutes is None:
            minutes = DEFAULT_MINUTES.get(kind)
        elif minutes not in MINUTES_ALLOWED:
            raise ProposalError(f"proposal {i}: minutes must be one of "
                                f"{', '.join(map(str, MINUTES_ALLOWED))}")
        p["minutes"] = minutes

        thing = item.get("thing")
        if thing is not None:
            if not isinstance(thing, dict):
                raise ProposalError(f"proposal {i}: thing must be an object")
            keys = [k for k in ("task_id", "plan_id", "label")
                    if thing.get(k) not in (None, "")]
            if len(keys) != 1:
                raise ProposalError(f"proposal {i}: thing needs exactly one "
                                    f"of task_id, plan_id, label")
            key = keys[0]
            if key == "task_id":
                tid = _int(thing["task_id"], f"proposal {i} thing.task_id")
                if tid not in ev.task_ids:
                    raise ProposalError(f"proposal {i}: task #{tid} is not on "
                                        f"today's list")
                p["thing"] = {"task_id": tid}
            elif key == "plan_id":
                pid = _int(thing["plan_id"], f"proposal {i} thing.plan_id")
                if pid not in ev.plan_ids:
                    raise ProposalError(f"proposal {i}: plan #{pid} is not in "
                                        f"the brief")
                p["thing"] = {"plan_id": pid}
            else:
                p["thing"] = {"label": _text(thing["label"], 80,
                                             f"proposal {i} thing.label")}

        step = item.get("prepared_step")
        if step is not None:
            if not isinstance(step, dict):
                raise ProposalError(f"proposal {i}: prepared_step must be an object")
            tid = _int(step.get("task_id"), f"proposal {i} prepared_step.task_id")
            if tid not in ev.task_ids:
                raise ProposalError(f"proposal {i}: prepared_step task #{tid} "
                                    f"is not on today's list")
            p["prepared_step"] = {
                "task_id": tid,
                "next_action": _text(step.get("next_action"), NEXT_ACTION_CAP,
                                     f"proposal {i} prepared_step.next_action"),
            }
        if action == "rest_here" and p["prepared_step"] is not None:
            raise ProposalError(f"proposal {i}: rest changes nothing - no "
                                f"prepared_step on rest_here")
        if action == "start_task":
            if not p["thing"] or "task_id" not in p["thing"]:
                raise ProposalError(f"proposal {i}: start_task needs thing.task_id")
            if not p["prepared_step"]:
                raise ProposalError(f"proposal {i}: start_task needs a "
                                    f"prepared_step with the next_action")
            if p["prepared_step"]["task_id"] != p["thing"]["task_id"]:
                raise ProposalError(f"proposal {i}: prepared_step must name "
                                    f"the same task as thing")

        codes = item.get("rationale_codes") or []
        if not isinstance(codes, list):
            raise ProposalError(f"proposal {i}: rationale_codes must be a list")
        for code in codes:
            if code not in ev.codes:
                raise ProposalError(f"proposal {i}: evidence id {code!r} is not "
                                    f"in the brief")
        p["rationale_codes"] = list(dict.fromkeys(codes))
        out.append(p)
    return out


def _int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProposalError(f"{name} must be an integer") from None


# --- the fallback ladder (section 6) ----------------------------------------

def fallback(ev: Evidence, exclude: Iterable[str] = ()) -> tuple[list[dict], bool]:
    """(proposals, exhausted): up to three kinds not in `exclude`, in the
    ladder's order - step (or company when no task can carry one), body,
    rest, then the reserves: company, thread, switch, sound. deterministic,
    model-free, copy from FALLBACK_COPY. fewer than three fresh kinds means
    the ladder is out (sol, #89): the caller marks the episode exhausted
    rather than shipping a thin card as if it were whole."""
    excluded = {k for k in exclude if k}
    out: list[dict] = []
    seen: set = set()
    for cand in _candidates(ev):
        if cand is None or cand["kind"] in excluded or cand["kind"] in seen:
            continue
        out.append(cand)
        seen.add(cand["kind"])
        if len(out) == PROPOSALS_PER_GENERATION:
            break
    return out, len(out) < PROPOSALS_PER_GENERATION


def _candidates(ev: Evidence):
    c = FALLBACK_COPY
    step = _fallback_step(ev)
    company = {"kind": "company", "action": "start_company",
               "line": c["company_line"], "enough": c["company_enough"],
               "minutes": 5, "why": None, "why_register": "matters",
               "thing": None, "prepared_step": None,
               "rationale_codes": [CODE_NO_SUITABLE_TASK] if not ev.tasks else []}
    yield step if step is not None else company
    hours_away = (CODE_HOURS_SINCE_LAST_RUN in ev.codes
                  or (CODE_NO_RUNS_TODAY in ev.codes and ev.now_local.hour >= 12))
    body = ({"kind": "body", "action": "pause_and_away",
             "line": c["body_out_line"], "enough": c["body_out_enough"],
             "minutes": 8}
            if hours_away else
            {"kind": "body", "action": "body_here",
             "line": c["body_here_line"], "enough": c["body_here_enough"],
             "minutes": None})
    body.update({"why": None, "why_register": "matters", "thing": None,
                 "prepared_step": None,
                 "rationale_codes": [x for x in (CODE_HOURS_SINCE_LAST_RUN,
                                                 CODE_NO_RUNS_TODAY)
                                     if x in ev.codes]})
    yield body
    sleepy = [x for x in (CODE_PAST_QUIET_HOURS, CODE_LATE_LAST_NIGHT)
              if x in ev.codes]
    yield {"kind": "rest", "action": "rest_here",
           "line": c["rest_sleep_line"] if sleepy else c["rest_here_line"],
           "enough": c["rest_sleep_enough"] if sleepy else c["rest_here_enough"],
           "minutes": None, "why": None, "why_register": "matters",
           "thing": None, "prepared_step": None, "rationale_codes": sleepy}
    # the reserves, for a second generation
    yield company
    yield _fallback_thread(ev)
    yield _fallback_switch(ev, step)
    yield {"kind": "sound", "action": "point_to_sound",
           "line": c["sound_line"], "enough": c["sound_enough"],
           "minutes": None, "why": None, "why_register": "matters",
           "thing": None, "prepared_step": None, "rationale_codes": []}


def _fallback_step(ev: Evidence) -> Optional[dict]:
    """the pressed row first (sol, #89), then a flagged task with a first
    piece, then the smallest untouched task."""
    c = FALLBACK_COPY
    focus = next((t for t in ev.tasks if t["id"] == ev.focus_task_id), None) \
        if ev.focus_task_id is not None else None
    flagged = [t for t in ev.tasks if t.get("signals") and t.get("next_action")]
    if focus is not None and focus.get("next_action"):
        t = focus
    elif focus is None and flagged:
        t = flagged[0]
    else:
        t = focus
    if t is not None:
        if t.get("next_action"):
            return {"kind": "step", "action": "start_task", "line": t["next_action"],
                    "enough": c["step_flagged_enough"], "minutes": 2, "why": None,
                    "why_register": "matters", "thing": {"task_id": t["id"]},
                    "prepared_step": {"task_id": t["id"],
                                      "next_action": t["next_action"]},
                    "rationale_codes": []}
    else:
        untouched = [x for x in ev.tasks if not x.get("banked_seconds")]
        pool = untouched or ev.tasks
        if not pool:
            return None
        t = min(pool, key=lambda x: (x.get("pom_estimate") or 99, x["id"]))
    line = c["step_open_line"].format(title=t["title"])
    return {"kind": "step", "action": "start_task", "line": line,
            "enough": c["step_open_enough"], "minutes": 2, "why": None,
            "why_register": "matters", "thing": {"task_id": t["id"]},
            "prepared_step": {"task_id": t["id"],
                              "next_action": t.get("next_action") or line[:NEXT_ACTION_CAP]},
            "rationale_codes": []}


def _fallback_thread(ev: Evidence) -> Optional[dict]:
    """pick up the thread: only honest when the last run was on a task
    still on the list."""
    if ev.last_run_task_id is None or ev.last_run_task_id not in ev.task_ids:
        return None
    c = FALLBACK_COPY
    label = ev.last_run_label or next(
        t["title"] for t in ev.tasks if t["id"] == ev.last_run_task_id)
    line = c["thread_line"].format(label=label)
    return {"kind": "thread", "action": "start_task", "line": line,
            "enough": c["thread_enough"], "minutes": 2, "why": None,
            "why_register": "matters", "thing": {"task_id": ev.last_run_task_id},
            "prepared_step": {"task_id": ev.last_run_task_id,
                              "next_action": line[:NEXT_ACTION_CAP]},
            "rationale_codes": []}


def _fallback_switch(ev: Evidence, step: Optional[dict]) -> Optional[dict]:
    """a different thing: the smallest OTHER task, when there is one."""
    taken = step["thing"]["task_id"] if step else None
    others = [t for t in ev.tasks if t["id"] != taken]
    if not others:
        return None
    c = FALLBACK_COPY
    t = min(others, key=lambda x: (x.get("pom_estimate") or 99, x["id"]))
    line = c["switch_line"].format(title=t["title"])
    return {"kind": "switch", "action": "start_task", "line": line,
            "enough": c["switch_enough"], "minutes": 2, "why": None,
            "why_register": "matters", "thing": {"task_id": t["id"]},
            "prepared_step": {"task_id": t["id"],
                              "next_action": t.get("next_action") or line[:NEXT_ACTION_CAP]},
            "rationale_codes": []}


# --- the episode store (5.1 + 7) ---------------------------------------------

class StuckStore:
    """every read and write of the two stuck tables. episodes are
    tenant-scoped by user_uuid on every query; the caller passes the
    identity it verified, never a body field."""

    # -- open ---------------------------------------------------------------

    def open(self, user_uuid: str, *, request_uuid: str, surface: str,
             device_id: Optional[int] = None,
             task_id: Optional[int] = None) -> tuple[dict, bool]:
        """(episode, created). a retried press replays the same episode:
        (device_id, request_uuid) is unique, and a surface without a
        device (rooms, telegram) dedupes on the user's open episodes with
        the same request instead."""
        with get_db() as db:
            q = db.query(StuckEpisode).filter(
                StuckEpisode.user_uuid == user_uuid,
                StuckEpisode.request_uuid == request_uuid)
            if device_id is not None:
                q = q.filter(StuckEpisode.device_id == device_id)
            existing = q.first()
            if existing is not None:
                return self._serialize(existing), False
            if task_id is not None:
                owned = db.query(Task.id).filter(
                    Task.user_uuid == user_uuid, Task.id == task_id).scalar()
                if owned is None:
                    raise ValueError("task not found")
            row = StuckEpisode(
                user_uuid=user_uuid, device_id=device_id,
                request_uuid=request_uuid, surface=surface, task_id=task_id,
                status=THINKING, generation=1, proposals=[],
                opened_at=utc_now())
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._serialize(row), True

    # -- read ---------------------------------------------------------------

    def get(self, user_uuid: str, episode_uuid: str) -> Optional[dict]:
        with get_db() as db:
            row = self._row(db, user_uuid, episode_uuid)
            return self._serialize(row) if row is not None else None

    def recent_summaries(self, user_uuid: str, limit: int = 5,
                         exclude_uuid: Optional[str] = None) -> list[str]:
        """one line per recent episode for the brief (5.2 item 5): what
        was accepted and what happened. raw evidence, never assessment."""
        with get_db() as db:
            q = db.query(StuckEpisode).filter(
                StuckEpisode.user_uuid == user_uuid)
            if exclude_uuid:
                q = q.filter(StuckEpisode.episode_uuid != exclude_uuid)
            rows = q.order_by(StuckEpisode.opened_at.desc()).limit(limit).all()
            out = []
            for r in rows:
                when = r.opened_at.strftime("%b %-d") if r.opened_at else "?"
                if r.status == ACCEPTED:
                    line = next((p.get("line") for p in (r.proposals or [])
                                 if p.get("proposal_id") == r.accepted_proposal_id),
                                None)
                    bit = f"{when}: accepted {r.accepted_kind}"
                    if line:
                        bit += f' ("{line}")'
                    outcome = r.outcome or {}
                    if outcome.get("banked_seconds"):
                        bit += f" - banked {outcome['banked_seconds'] // 60} min"
                    if outcome.get("stopped_at_boundary"):
                        bit += ", stopped at the boundary"
                elif r.status == RESTED:
                    bit = f"{when}: chose rest"
                elif r.status == CLOSED:
                    bit = f"{when}: closed without choosing"
                else:
                    continue
                out.append(bit)
            return out

    # -- the turn's writes ----------------------------------------------------

    def settle(self, episode_uuid: str, generation: int, proposals: list[dict],
               source: str, exhausted: bool = False) -> Optional[dict]:
        """the one model-facing write: proposals for THIS generation land
        only while the episode is still thinking at that generation. a
        late turn - after the fallback stood in, after the person chose -
        is refused (None), never applied. the write is a CONDITIONAL update
        on (status, generation): two writers racing (a timed-out tool
        thread that kept running, and the ladder) can both read `thinking`,
        but only one statement matches (sol, #89). ids are assigned here."""
        status = READY if source == "model" else FALLBACK
        with get_db() as db:
            row = db.query(StuckEpisode).filter(
                StuckEpisode.episode_uuid == episode_uuid).first()
            if row is None:
                return None
            if row.status != THINKING or row.generation != generation:
                return None
            stamped = []
            for p in proposals:
                stamped.append({**p, "proposal_id": str(uuid_mod.uuid4()),
                                "generation": generation, "source": source})
            updated = db.query(StuckEpisode).filter(
                StuckEpisode.id == row.id,
                StuckEpisode.status == THINKING,
                StuckEpisode.generation == generation,
            ).update({
                StuckEpisode.proposals: list(row.proposals or []) + stamped,
                StuckEpisode.status: status,
                StuckEpisode.ready_at: row.ready_at or utc_now(),
                StuckEpisode.exhausted: bool(exhausted),
            }, synchronize_session=False)
            db.commit()
            if not updated:
                return None
            db.expire_all()
            return self._serialize(db.get(StuckEpisode, row.id))

    def fail(self, episode_uuid: str, generation: int, error: str) -> Optional[dict]:
        """a turn that can't even fall back (no snapshot at all). terminal;
        conditional like settle."""
        with get_db() as db:
            row = db.query(StuckEpisode).filter(
                StuckEpisode.episode_uuid == episode_uuid).first()
            if row is None:
                return None
            updated = db.query(StuckEpisode).filter(
                StuckEpisode.id == row.id,
                StuckEpisode.status == THINKING,
                StuckEpisode.generation == generation,
            ).update({StuckEpisode.status: FAILED,
                      StuckEpisode.error: error[:300],
                      StuckEpisode.closed_at: utc_now()},
                     synchronize_session=False)
            db.commit()
            if not updated:
                return None
            db.expire_all()
            return self._serialize(db.get(StuckEpisode, row.id))

    def list_thinking(self) -> list[tuple[str, dict]]:
        """every open episode still waiting for a card - what a restarted
        server must resume (sol, #89): the row is the durable claim, the
        in-memory task is only its runner."""
        with get_db() as db:
            rows = db.query(StuckEpisode).filter(
                StuckEpisode.status == THINKING,
                StuckEpisode.closed_at.is_(None)).all()
            return [(r.user_uuid, self._serialize(r)) for r in rows]

    # -- reactions (5.1 transitions) ------------------------------------------

    def react(self, user_uuid: str, episode_uuid: str, *, request_uuid: str,
              generation: int, reaction: str,
              proposal_id: Optional[str] = None) -> tuple[dict, str]:
        """(episode, outcome) where outcome is one of: replay, recorded,
        regenerate (a new generation is owed), exhausted (no more
        generations - the client shows rest), accepted, rested, closed.
        raises ValueError with the reason (not found / settled / stale
        generation / unknown proposal)."""
        if reaction not in REACTIONS:
            raise ValueError("unknown reaction")
        with get_db() as db:
            prior = db.query(StuckReaction).filter(
                StuckReaction.request_uuid == request_uuid).first()
            row = self._row(db, user_uuid, episode_uuid)
            if row is None:
                raise ValueError("episode not found")
            if prior is not None:
                if prior.episode_id != row.id:
                    raise ValueError("request id belongs to another episode")
                return self._serialize(row), "replay"
            if row.status in TERMINAL:
                raise ValueError("episode is settled")
            if generation != row.generation:
                raise ValueError("stale generation")
            current = [p for p in (row.proposals or [])
                       if p.get("generation") == row.generation]
            chosen = None
            if reaction in ("accepted", "different"):
                if row.status not in CARD_SHOWING:
                    raise ValueError("no card to react to yet")
                chosen = next((p for p in current
                               if p.get("proposal_id") == proposal_id), None)
                if chosen is None:
                    raise ValueError("unknown proposal")
            db.add(StuckReaction(
                episode_id=row.id, user_uuid=user_uuid,
                proposal_id=proposal_id if chosen else None,
                generation=row.generation, reaction=reaction,
                request_uuid=request_uuid, created_at=utc_now()))
            now = utc_now()
            outcome = "recorded"
            # every transition is a CONDITIONAL update on the (status,
            # generation) this reaction was read against: two windows
            # reacting to the same card can both pass the checks above,
            # but only one statement matches; the other's reaction row
            # rolls back with it and the caller gets a conflict (sol, #89)
            guard = [StuckEpisode.id == row.id,
                     StuckEpisode.generation == row.generation]
            if reaction == "accepted":
                self._prepare(db, user_uuid, chosen)
                updated = db.query(StuckEpisode).filter(
                    *guard, StuckEpisode.status.in_(CARD_SHOWING)
                ).update({StuckEpisode.status: ACCEPTED,
                          StuckEpisode.accepted_proposal_id: chosen["proposal_id"],
                          StuckEpisode.accepted_kind: chosen["kind"],
                          StuckEpisode.execution_id: str(uuid_mod.uuid4()),
                          StuckEpisode.closed_at: now},
                         synchronize_session=False)
                outcome = "accepted"
            elif reaction == "too_much":
                updated = db.query(StuckEpisode).filter(
                    *guard, StuckEpisode.status.notin_(TERMINAL)
                ).update({StuckEpisode.status: RESTED,
                          StuckEpisode.closed_at: now},
                         synchronize_session=False)
                outcome = "rested"
            elif reaction == "closed":
                updated = db.query(StuckEpisode).filter(
                    *guard, StuckEpisode.status.notin_(TERMINAL)
                ).update({StuckEpisode.status: CLOSED,
                          StuckEpisode.closed_at: now},
                         synchronize_session=False)
                outcome = "closed"
            else:   # different
                db.flush()
                rejected = {r.proposal_id for r in db.query(StuckReaction).filter(
                    StuckReaction.episode_id == row.id,
                    StuckReaction.generation == row.generation,
                    StuckReaction.reaction == "different").all()}
                updated = 1
                if all(p.get("proposal_id") in rejected for p in current):
                    if row.generation < MAX_GENERATIONS:
                        updated = db.query(StuckEpisode).filter(
                            *guard, StuckEpisode.status.in_(CARD_SHOWING)
                        ).update({StuckEpisode.generation: row.generation + 1,
                                  StuckEpisode.status: THINKING},
                                 synchronize_session=False)
                        outcome = "regenerate"
                    else:
                        updated = db.query(StuckEpisode).filter(
                            *guard, StuckEpisode.status.in_(CARD_SHOWING)
                        ).update({StuckEpisode.exhausted: True},
                                 synchronize_session=False)
                        outcome = "exhausted"
            if not updated:
                db.rollback()
                raise ValueError("the card changed under you - refresh it")
            db.commit()
            db.expire_all()
            return self._serialize(db.get(StuckEpisode, row.id)), outcome

    @staticmethod
    def _prepare(db, user_uuid: str, proposal: dict) -> None:
        """the accept-time preparation (section 4): the ONE next_action the
        visible action names, written in the same transaction that claims
        the proposal. rest carries none, ever. the task is re-checked at
        this moment - a card generated an hour ago may name a task that
        another window since finished or parked (sol, #89); a stale card
        is a conflict, never a write onto a closed task."""
        if proposal.get("action") == "rest_here":
            return
        step = proposal.get("prepared_step")
        thing = proposal.get("thing") or {}
        task_id = (step or {}).get("task_id") or thing.get("task_id")
        if task_id is None:
            return
        from src.services.workspace.agenda import user_today
        task = db.query(Task).filter(Task.user_uuid == user_uuid,
                                     Task.id == task_id).first()
        if task is None:
            raise ValueError("prepared task not found")
        today = user_today(user_uuid)
        if task.status not in vocab.TASK_STATUS_OPEN:
            raise ValueError("that task was finished since the card was made "
                             "- refresh it")
        if task.set_aside_on is not None and task.set_aside_on >= today:
            raise ValueError("that task was set aside since the card was made "
                             "- refresh it")
        if not step:
            return
        value = " ".join(str(step["next_action"]).split())[:NEXT_ACTION_CAP].strip()
        task.next_action = value or None
        task.updated_at = utc_now()

    # -- outcomes + hygiene ---------------------------------------------------

    def record_outcome(self, user_uuid: str, episode_uuid: str, **fields) -> Optional[dict]:
        """fold what the device stream said into `outcome` (section 7).
        merges; never asks."""
        with get_db() as db:
            row = self._row(db, user_uuid, episode_uuid)
            if row is None:
                return None
            row.outcome = {**(row.outcome or {}), **fields}
            db.commit()
            db.refresh(row)
            return self._serialize(row)

    def sweep(self, ttl_minutes: int, now: Optional[datetime] = None) -> int:
        """close open episodes nobody touched for TTL minutes, so
        correctness never depends on an unload request arriving (5.1)."""
        now = now or utc_now()
        cutoff = now - timedelta(minutes=ttl_minutes)
        with get_db() as db:
            # two statements, each atomic: a reaction landing between them
            # sees closed_at set and conflicts, never a half-swept row
            failed = db.query(StuckEpisode).filter(
                StuckEpisode.closed_at.is_(None),
                StuckEpisode.opened_at < cutoff,
                StuckEpisode.status == THINKING,
            ).update({StuckEpisode.status: FAILED,
                      StuckEpisode.error: "swept while thinking",
                      StuckEpisode.closed_at: now}, synchronize_session=False)
            closed = db.query(StuckEpisode).filter(
                StuckEpisode.closed_at.is_(None),
                StuckEpisode.opened_at < cutoff,
                StuckEpisode.status.notin_(TERMINAL),
            ).update({StuckEpisode.status: CLOSED,
                      StuckEpisode.closed_at: now}, synchronize_session=False)
            db.commit()
            return int(failed or 0) + int(closed or 0)

    # -- shapes ---------------------------------------------------------------

    @staticmethod
    def _row(db, user_uuid: str, episode_uuid: str) -> Optional[StuckEpisode]:
        return db.query(StuckEpisode).filter(
            StuckEpisode.user_uuid == user_uuid,
            StuckEpisode.episode_uuid == episode_uuid).first()

    def _serialize(self, row: StuckEpisode) -> dict:
        proposals = [p for p in (row.proposals or [])
                     if p.get("generation") == row.generation]
        rejected = []
        with get_db() as db:
            for r in db.query(StuckReaction).filter(
                    StuckReaction.episode_id == row.id,
                    StuckReaction.generation == row.generation,
                    StuckReaction.reaction == "different").all():
                if r.proposal_id:
                    rejected.append(r.proposal_id)
        rejected_kinds = list(dict.fromkeys(
            p["kind"] for p in (row.proposals or [])
            if p.get("generation", 0) < row.generation))
        exhausted = bool(row.exhausted) or (
            row.status in CARD_SHOWING and row.generation >= MAX_GENERATIONS
            and bool(proposals)
            and all(p["proposal_id"] in rejected for p in proposals))
        accepted = next((p for p in (row.proposals or [])
                         if p.get("proposal_id") == row.accepted_proposal_id), None)
        return {
            "episode_id": row.episode_uuid,
            "status": row.status,
            "generation": row.generation,
            "surface": row.surface,
            "task_id": row.task_id,
            "source": proposals[0].get("source") if proposals else None,
            "proposals": proposals,
            "rejected_proposal_ids": rejected,
            # earlier generations' kinds: what the next turn must not offer (2.3)
            "rejected_kinds": rejected_kinds,
            "exhausted": exhausted,
            "accepted_proposal_id": row.accepted_proposal_id,
            "execution": execution_for(row, accepted) if accepted else None,
            "error": row.error,
            "opened_at": _iso(row.opened_at),
            "ready_at": _iso(row.ready_at),
            "closed_at": _iso(row.closed_at),
        }


# --- outcomes, folded from the device stream (section 7) --------------------
# a stuck run's session.started / session.ended carry the episode and
# execution ids; the fold matches them against the episode's own
# execution_id and merges what happened into `outcome`. nobody is asked.

FOLDED_TYPES = frozenset({"session.started", "session.ended"})
REASON_BOUNDARY = "boundary"
# the v0 resume point (section 4): the step they did, marked as begun -
# written here, from the durable session.ended, so a dead network or a
# closed window can't lose it (sol, #92). one writer: the server.
RESUME_PREFIX = "pick up where you stopped: "


def resume_point(step: Optional[str]) -> str:
    clean = " ".join((step or "").split())
    if not clean:
        return RESUME_PREFIX.rstrip(": ")
    room = NEXT_ACTION_CAP - len(RESUME_PREFIX)
    if len(clean) > room:
        clean = clean[:room - 1].rstrip() + "…"
    return RESUME_PREFIX + clean


def fold_event(db, row) -> None:
    """runs inside focus_flow's claimed transaction, like the rewind
    tether's fold. a plain run (no ids) is not ours; an id that doesn't
    match the episode's execution is a stale or foreign run and is logged,
    never applied."""
    payload = row.payload or {}
    episode_uuid = payload.get("episode_id")
    execution_id = payload.get("execution_id")
    if not episode_uuid or not execution_id:
        return
    ep = db.query(StuckEpisode).filter(
        StuckEpisode.user_uuid == row.user_uuid,
        StuckEpisode.episode_uuid == episode_uuid).first()
    if ep is None:
        logger.warning("stuck fold: no episode %s for %s", episode_uuid, row.event_uuid)
        return
    if ep.execution_id != execution_id:
        logger.warning("stuck fold: execution %s is not episode %s's (%s)",
                       execution_id, episode_uuid, ep.execution_id)
        return
    outcome = dict(ep.outcome or {})
    if row.event_type == "session.started":
        ep.run_ref = {"device_id": row.device_id, "event_uuid": row.event_uuid}
        outcome["started"] = True
    else:
        seconds = int(payload.get("seconds") or 0)
        reason = payload.get("reason")
        outcome["banked_seconds"] = int(outcome.get("banked_seconds") or 0) + seconds
        outcome["reason"] = reason
        outcome["stopped_at_boundary"] = reason == REASON_BOUNDARY
        target = payload.get("target_minutes")
        try:
            target_seconds = int(float(target) * 60) if target is not None else None
        except (TypeError, ValueError):
            target_seconds = None
        if target_seconds is not None:
            outcome["kept_going"] = (reason != REASON_BOUNDARY
                                     and seconds > target_seconds)
        if reason == REASON_BOUNDARY:
            written = _write_resume_point(db, ep)
            if written is not None:
                outcome["resume_point"] = written
    ep.outcome = outcome


def _write_resume_point(db, ep: StuckEpisode) -> Optional[str]:
    """the accepted proposal's prepared step becomes the resume point -
    only while the task still carries that step untouched. a step the
    person already rewrote is theirs; a task since closed is left alone."""
    accepted = next((p for p in (ep.proposals or [])
                     if p.get("proposal_id") == ep.accepted_proposal_id), None)
    step = (accepted or {}).get("prepared_step") or {}
    if not step.get("task_id") or not step.get("next_action"):
        return None
    task = db.query(Task).filter(Task.user_uuid == ep.user_uuid,
                                 Task.id == step["task_id"]).first()
    if task is None or task.status not in vocab.TASK_STATUS_OPEN:
        return None
    current = " ".join((task.next_action or "").split())
    expected = " ".join(str(step["next_action"]).split())[:NEXT_ACTION_CAP].strip()
    if current != expected:
        return None
    value = resume_point(expected)
    task.next_action = value
    task.updated_at = utc_now()
    return value


def execution_for(row: StuckEpisode, proposal: dict) -> dict:
    """the typed handoff the sidecar deduplicates on (section 4 + 5.1):
    what to do locally, exactly as the visible action said."""
    step = proposal.get("prepared_step") or {}
    thing = proposal.get("thing") or {}
    return {
        "execution_id": row.execution_id,
        "episode_id": row.episode_uuid,
        "action": proposal["action"],
        "kind": proposal["kind"],
        "task_id": step.get("task_id") or thing.get("task_id"),
        "next_action": step.get("next_action"),
        "minutes": proposal.get("minutes"),
        "line": proposal.get("line"),
    }


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


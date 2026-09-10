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
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from config import Config
from src.database.database import get_db
from src.database.models import StuckEpisode, StuckReaction, Task
from src.services.scorecard import _late_seconds
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


def gather(user_uuid: str, today=None, recent: Iterable[dict] = ()) -> Evidence:
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


def _late_seconds_around(fd, user_uuid: str) -> int:
    """seconds of yesterday's and today's runs inside [23:00, 06:00) local -
    the one piece of last night the afternoon should know about. guarded:
    yesterday's read failing costs the code, never the episode."""
    from src.services import focus_day
    runs = list(fd.runs)
    try:
        runs += list(focus_day.snapshot(user_uuid, fd.day - timedelta(days=1)).runs)
    except Exception:
        logger.exception("yesterday's snapshot failed for %s", user_uuid)
    total = 0
    for r in runs:
        end = r.ended_at
        start = end - timedelta(seconds=max(0, int(r.seconds or 0)))
        total += _late_seconds(start, end)
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

def fallback(ev: Evidence) -> list[dict]:
    """three kinds, deterministic, model-free. the slot for a step becomes
    company when no task can carry one; body reads hours-since; rest reads
    the night. copy from FALLBACK_COPY, never generated."""
    c = FALLBACK_COPY
    first = _fallback_step(ev)
    if first is None:
        first = {"kind": "company", "action": "start_company",
                 "line": c["company_line"], "enough": c["company_enough"],
                 "minutes": 5, "why": None, "why_register": "matters",
                 "thing": None, "prepared_step": None,
                 "rationale_codes": [CODE_NO_SUITABLE_TASK] if not ev.tasks else []}
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
    sleepy = [x for x in (CODE_PAST_QUIET_HOURS, CODE_LATE_LAST_NIGHT)
              if x in ev.codes]
    rest = {"kind": "rest", "action": "rest_here",
            "line": c["rest_sleep_line"] if sleepy else c["rest_here_line"],
            "enough": c["rest_sleep_enough"] if sleepy else c["rest_here_enough"],
            "minutes": None, "why": None, "why_register": "matters",
            "thing": None, "prepared_step": None, "rationale_codes": sleepy}
    return [first, body, rest]


def _fallback_step(ev: Evidence) -> Optional[dict]:
    c = FALLBACK_COPY
    flagged = [t for t in ev.tasks if t.get("signals") and t.get("next_action")]
    if flagged:
        t = flagged[0]
        return {"kind": "step", "action": "start_task", "line": t["next_action"],
                "enough": c["step_flagged_enough"], "minutes": 2, "why": None,
                "why_register": "matters", "thing": {"task_id": t["id"]},
                "prepared_step": {"task_id": t["id"],
                                  "next_action": t["next_action"]},
                "rationale_codes": []}
    untouched = [t for t in ev.tasks if not t.get("banked_seconds")]
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
               source: str) -> Optional[dict]:
        """the one model-facing write: proposals for THIS generation land
        only while the episode is still thinking at that generation. a
        late turn - after the fallback stood in, after the person chose -
        is refused (None), never applied. ids are assigned here."""
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
            row.proposals = list(row.proposals or []) + stamped
            row.status = status
            if row.ready_at is None:
                row.ready_at = utc_now()
            db.commit()
            db.refresh(row)
            return self._serialize(row)

    def fail(self, episode_uuid: str, generation: int, error: str) -> Optional[dict]:
        """a turn that can't even fall back (no snapshot at all). terminal."""
        with get_db() as db:
            row = db.query(StuckEpisode).filter(
                StuckEpisode.episode_uuid == episode_uuid).first()
            if row is None or row.status != THINKING or row.generation != generation:
                return None
            row.status = FAILED
            row.error = error[:300]
            row.closed_at = utc_now()
            db.commit()
            db.refresh(row)
            return self._serialize(row)

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
            if reaction == "accepted":
                row.status = ACCEPTED
                row.accepted_proposal_id = chosen["proposal_id"]
                row.accepted_kind = chosen["kind"]
                row.execution_id = str(uuid_mod.uuid4())
                row.closed_at = now
                self._prepare(db, user_uuid, chosen)
                outcome = "accepted"
            elif reaction == "too_much":
                row.status = RESTED
                row.closed_at = now
                outcome = "rested"
            elif reaction == "closed":
                row.status = CLOSED
                row.closed_at = now
                outcome = "closed"
            else:   # different
                db.flush()
                rejected = {r.proposal_id for r in db.query(StuckReaction).filter(
                    StuckReaction.episode_id == row.id,
                    StuckReaction.generation == row.generation,
                    StuckReaction.reaction == "different").all()}
                if all(p.get("proposal_id") in rejected for p in current):
                    if row.generation < MAX_GENERATIONS:
                        row.generation += 1
                        row.status = THINKING
                        outcome = "regenerate"
                    else:
                        outcome = "exhausted"
            db.commit()
            db.refresh(row)
            return self._serialize(row), outcome

    @staticmethod
    def _prepare(db, user_uuid: str, proposal: dict) -> None:
        """the accept-time preparation (section 4): the ONE next_action the
        visible action names, written in the same transaction that claims
        the proposal. the same cap the store applies to any scope."""
        step = proposal.get("prepared_step")
        if not step:
            return
        task = db.query(Task).filter(Task.user_uuid == user_uuid,
                                     Task.id == step["task_id"]).first()
        if task is None:
            raise ValueError("prepared task not found")
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
            rows = db.query(StuckEpisode).filter(
                StuckEpisode.closed_at.is_(None),
                StuckEpisode.opened_at < cutoff).all()
            for row in rows:
                if row.status == THINKING:
                    row.status = FAILED
                    row.error = "swept while thinking"
                else:
                    row.status = CLOSED
                row.closed_at = now
            db.commit()
            return len(rows)

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
        exhausted = (row.status in CARD_SHOWING and row.generation >= MAX_GENERATIONS
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


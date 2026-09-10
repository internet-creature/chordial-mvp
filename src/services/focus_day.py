"""vel's sense of the day (docs/FOCUS_DOGFOOD_DESIGN.md §5, §10.2).

before this, the proactive prompt carried zero focus data: no clock, no
banked minutes, no blocks, no finished-today tasks - vel wrote the same
check-in whether the person was eighteen minutes into a block or hadn't
started anything. this module is the READ MODEL behind her awareness:

- `snapshot(user_uuid, day)` folds the user's applied device events for
  one user-local day (session.started / session.ended / focus_block.completed
  / drift.detected / return.detected / rewind.applied) together with the
  workspace (tasks finished that day, tasks set aside, the day's plan) into
  a `FocusDay`. a device whose latest session event is a session.started
  has a clock running since then.
- `render(day)` is the digest: the ambient block every daily turn sees -
  scheduled ticks AND ordinary user turns, so she knows the day when you
  talk to her, not only when she reaches out. minutes, runs, finishes,
  drifts. never streaks, phases, scores, or assessments (ROOMS_DESIGN §5:
  no coach pressure in ambient presence holds for what the deer SEES as
  much as for what she says).
- `checkin_posture(day)` picks, deterministically and server-side, the ONE
  instruction shape a scheduled tick renders (§5.3): mid_block / stuck /
  untouched / between / wrapped / quiet_day. the prompt only renders it.
- `breakdown_flags(user_uuid)` is the evidence nudge (§10.2): which open
  tasks keep waiting - moved twice, parked on two days, two short starts
  without a scope, untouched two days running - surfaced as
  `needs_breakdown` on the today payload's rows and as the `stuck` posture.

pure db reads. every caller guards: a failure here costs the block or the
posture, never the turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func

from config import Config
from src.database.database import get_db
from src.database.models import DeviceEvent, Task, TaskSetAside, User
from src.services.workspace import vocab
from src.utils.timezone_utils import (
    local_day_bounds,
    to_user_timezone,
    utc_now,
)

logger = logging.getLogger(__name__)

# the event types the day is made of
_SESSION_TYPES = ("session.started", "session.frozen", "session.ended")
_DAY_TYPES = _SESSION_TYPES + ("focus_block.completed", "drift.detected",
                               "return.detected", "rewind.applied")

# §10.2 thresholds. a run credited under this many seconds is a "short
# start" - the initiation-failure signal counts two of them on an unscoped
# task in one day (the server can't see a skipped scoping form; "still
# unscoped after two false starts" is the observable version)
SHORT_RUN_SECONDS = 5 * 60
RESCHEDULES_THRESHOLD = 2
SET_ASIDE_DAYS_THRESHOLD = 2
SHORT_STARTS_THRESHOLD = 2

# digest caps: task lines, like the agenda
_LINE_CAP = 6

POSTURE_MID_BLOCK = "mid_block"
POSTURE_STUCK = "stuck"
POSTURE_UNTOUCHED = "untouched"
POSTURE_BETWEEN = "between"
POSTURE_WRAPPED = "wrapped"
POSTURE_QUIET_DAY = "quiet_day"
DAY_POSTURES = frozenset({
    POSTURE_MID_BLOCK, POSTURE_STUCK, POSTURE_UNTOUCHED, POSTURE_BETWEEN,
    POSTURE_WRAPPED, POSTURE_QUIET_DAY,
})


@dataclass(frozen=True)
class Run:
    """one banked run (a session.ended), user-local."""
    task_id: Optional[int]
    label: str
    seconds: int          # credited - what banks, untouched
    ended_at: datetime    # user-local naive
    reason: str


@dataclass(frozen=True)
class Running:
    """a clock that is running right now (a session.started with no
    session.ended after it on that device)."""
    task_id: Optional[int]
    label: str
    since: datetime       # user-local naive
    minutes_in: int
    target_minutes: Optional[int]


@dataclass
class FocusDay:
    day: date
    live: bool              # `day` is the user's today: running/idle mean something
    now: datetime           # user-local naive
    runs: list = field(default_factory=list)            # [Run], in order
    banked_by_task: dict = field(default_factory=dict)  # task key -> seconds
    titles: dict = field(default_factory=dict)          # task_id -> canonical title
    running: Optional[Running] = None
    frozen: Optional[Running] = None    # a stopped clock waiting on a rewind answer
    finished: list = field(default_factory=list)        # titles closed that day
    set_aside: list = field(default_factory=list)       # titles parked that day
    planned: list = field(default_factory=list)         # open today/overdue rows (dicts)
    untouched: list = field(default_factory=list)       # planned titles with 0 banked
    drifts: int = 0
    returns: int = 0
    rewinds: int = 0
    blocks: int = 0
    flags: dict = field(default_factory=dict)           # task_id -> [signal, ...]

    @property
    def banked_seconds(self) -> int:
        return sum(r.seconds for r in self.runs)

    @property
    def run_count(self) -> int:
        return len(self.runs)

    @property
    def last_run(self) -> Optional[Run]:
        return self.runs[-1] if self.runs else None

    @property
    def idle_minutes(self) -> Optional[int]:
        """minutes since the last run ended; None while a clock runs or
        when nothing ran."""
        if self.running or not self.runs:
            return None
        return max(0, int((self.now - self.runs[-1].ended_at)
                          .total_seconds() // 60))

    @property
    def happened(self) -> bool:
        return bool(self.runs or self.running or self.frozen
                    or self.finished or self.set_aside)

    def title_of(self, task_id: Optional[int], label: str = "") -> str:
        if task_id is not None and task_id in self.titles:
            return self.titles[task_id]
        return label or "a task"


# --- the snapshot --------------------------------------------------------------


def _user_tz(db, user_uuid: str) -> str:
    user = db.query(User).filter(User.uuid == user_uuid).first()
    return user.timezone if user and user.timezone else "UTC"


def _seconds(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _task_key(payload: dict):
    task_id = payload.get("task_id")
    if isinstance(task_id, int) and not isinstance(task_id, bool):
        return task_id
    return payload.get("label") or "?"


def snapshot(user_uuid: str, day: Optional[date] = None) -> FocusDay:
    """the day as the server can see it. `day` defaults to the user's
    today; the morning brief passes yesterday. events are read from the
    START OF THE PREVIOUS local day (the untouched-two-days signal needs
    yesterday's banks, and a clock that started late last night and never
    ended is still running now) - a session.started older than that is
    treated as not running, so a device that died with a clock open
    stops haunting the digest after a day."""
    with get_db() as db:
        tz = _user_tz(db, user_uuid)
        now_local = to_user_timezone(utc_now(), tz)
        if day is None:
            day = now_local.date()
        fd = FocusDay(day=day, live=(day == now_local.date()), now=now_local)
        day_start, day_end = local_day_bounds(day, tz)
        prev_start, _ = local_day_bounds(day - timedelta(days=1), tz)

        when_col = func.coalesce(DeviceEvent.occurred_at,
                                 DeviceEvent.applied_at)
        rows = (db.query(DeviceEvent)
                .filter(DeviceEvent.user_uuid == user_uuid,
                        DeviceEvent.rejected.is_(False),
                        DeviceEvent.event_type.in_(_DAY_TYPES),
                        when_col >= prev_start,
                        when_col < day_end)
                .order_by(DeviceEvent.device_id, DeviceEvent.seq)
                .all())

        banked_prev: dict = {}        # task key -> seconds, the previous day
        short_starts: dict = {}       # task key -> count of short runs, this day
        last_session: dict = {}       # device_id -> (state, row, when)
        open_since: dict = {}         # device_id -> when its open run started
        for row in rows:
            payload = row.payload or {}
            when = row.occurred_at or row.applied_at
            if when is None:
                continue
            in_day = day_start <= when < day_end
            kind = row.event_type
            if kind == "session.ended":
                secs = _seconds(payload.get("seconds"))
                last_session[row.device_id] = ("ended", row, when)
                if secs is None:
                    continue
                key = _task_key(payload)
                if in_day:
                    fd.runs.append(Run(
                        task_id=key if isinstance(key, int) else None,
                        label=str(payload.get("label") or ""),
                        seconds=secs,
                        ended_at=to_user_timezone(when, tz),
                        reason=str(payload.get("reason") or "stopped")))
                    fd.banked_by_task[key] = fd.banked_by_task.get(key, 0) + secs
                    if secs < SHORT_RUN_SECONDS:
                        short_starts[key] = short_starts.get(key, 0) + 1
                else:
                    banked_prev[key] = banked_prev.get(key, 0) + secs
            elif kind == "session.started":
                last_session[row.device_id] = ("started", row, when)
                open_since[row.device_id] = when
            elif kind == "session.frozen":
                # the clock stopped without banking (an open rewind
                # question); the session.ended follows when it resolves.
                # a started with no transition after it is NOT a running
                # clock if this came after (sol, #88)
                last_session[row.device_id] = ("frozen", row, when)
            elif not in_day:
                continue
            elif kind == "focus_block.completed":
                fd.blocks += 1
            elif kind == "drift.detected":
                fd.drifts += 1
            elif kind == "return.detected":
                fd.returns += 1
            elif kind == "rewind.applied":
                fd.rewinds += 1
        # runs arrive per device in seq order; the day reads them in time order
        fd.runs.sort(key=lambda r: r.ended_at)

        if fd.live:
            # the most recent open clock across devices is "right now"; a
            # frozen one is reported as stopped, not running
            def _clock(row, when, until) -> Running:
                payload = row.payload or {}
                started = open_since.get(row.device_id, when)
                since = to_user_timezone(started, tz)
                target = payload.get("target_minutes")
                target = (int(target) if isinstance(target, (int, float))
                          and not isinstance(target, bool) and target > 0
                          else None)
                key = _task_key(payload)
                return Running(
                    task_id=key if isinstance(key, int) else None,
                    label=str(payload.get("label") or ""),
                    since=since,
                    minutes_in=max(0, int((until - started)
                                          .total_seconds() // 60)),
                    target_minutes=target)

            open_clocks = [(when, row) for state, row, when
                           in last_session.values() if state == "started"]
            if open_clocks:
                when, row = max(open_clocks, key=lambda p: p[0])
                fd.running = _clock(row, when, utc_now())
            frozen = [(when, row) for state, row, when
                      in last_session.values() if state == "frozen"]
            if frozen and not open_clocks:
                when, row = max(frozen, key=lambda p: p[0])
                fd.frozen = _clock(row, when, when)

        # the workspace side: every open task, plus the titles of whatever
        # the events named (closed since, or never open today)
        event_ids = {k for k in list(fd.banked_by_task) + list(banked_prev)
                     if isinstance(k, int)}
        if fd.running and fd.running.task_id is not None:
            event_ids.add(fd.running.task_id)
        # set aside is read from the LEDGER, not the task's current stamp:
        # "tomorrow" and "bring back" clear the stamp, and yesterday's
        # digest must still say what was consciously parked (sol, #88).
        # the stamp is unioned in for rows that predate the ledger
        parked = {tid for (tid,) in db.query(TaskSetAside.task_id).filter(
            TaskSetAside.user_uuid == user_uuid, TaskSetAside.day == day)}
        parked_prev = {tid for (tid,) in db.query(TaskSetAside.task_id).filter(
            TaskSetAside.user_uuid == user_uuid,
            TaskSetAside.day == day - timedelta(days=1))}
        event_ids |= parked
        tasks = db.query(Task).filter(
            Task.user_uuid == user_uuid,
            Task.status.in_(vocab.TASK_STATUS_OPEN)).all()
        seen = {t.id for t in tasks}
        missing = event_ids - seen
        if missing:
            tasks += db.query(Task).filter(
                Task.user_uuid == user_uuid, Task.id.in_(missing)).all()
        for t in tasks:
            fd.titles[t.id] = t.title

        finished = (db.query(Task)
                    .filter(Task.user_uuid == user_uuid,
                            Task.status == "done",
                            Task.closed_at.isnot(None),
                            Task.closed_at >= day_start,
                            Task.closed_at < day_end)
                    .order_by(Task.closed_at).all())
        fd.finished = [t.title for t in finished]
        for t in finished:
            fd.titles.setdefault(t.id, t.title)

        yesterday = day - timedelta(days=1)
        for t in sorted(tasks, key=lambda t: (t.scheduled is None,
                                              t.scheduled or day, t.id)):
            if t.id in parked or t.set_aside_on == day:
                # parked for the day: their call, out of every nudge -
                # listed even if it has since closed or been moved
                fd.set_aside.append(t.title)
                continue
            if t.status not in vocab.TASK_STATUS_OPEN:
                continue
            on_list = t.scheduled is not None and t.scheduled <= day
            banked_today = fd.banked_by_task.get(t.id, 0)
            if on_list:
                fd.planned.append(_planned_row(t, banked_today))
                running_on = fd.running and fd.running.task_id == t.id
                if not banked_today and not running_on:
                    fd.untouched.append(t.title)
            if t.breakdown_offer_dismissed_at is not None:
                continue
            signals = []
            if (t.reschedules or 0) >= RESCHEDULES_THRESHOLD:
                signals.append(f"moved {t.reschedules} times")
            if (t.set_aside_count or 0) >= SET_ASIDE_DAYS_THRESHOLD:
                signals.append(f"set aside on {t.set_aside_count} days")
            if not t.next_action and \
                    short_starts.get(t.id, 0) >= SHORT_STARTS_THRESHOLD:
                signals.append(f"{short_starts[t.id]} short starts today, "
                               "no first piece named")
            # "on the list two days": due by yesterday AND already existing
            # by the start of today AND not parked yesterday - a task
            # created (or backdated) today, or one consciously set aside
            # yesterday, was not waiting on yesterday's list (sol, #88).
            # a backdated `scheduled` on an older task is invisible here
            if (t.scheduled is not None and t.scheduled <= yesterday
                    and t.created_at is not None and t.created_at < day_start
                    and t.id not in parked_prev
                    and not banked_today and not banked_prev.get(t.id, 0)):
                signals.append("on the list two days, untouched")
            if signals:
                fd.flags[t.id] = signals
        return fd


def _planned_row(t: Task, banked_today: int) -> dict:
    return {
        "id": t.id, "title": t.title, "priority": t.priority,
        "scheduled": t.scheduled.isoformat() if t.scheduled else None,
        "pom_estimate": t.pom_estimate, "next_action": t.next_action,
        "banked_seconds": banked_today,
    }


def breakdown_flags(user_uuid: str) -> dict:
    """task_id -> signals for today, for the payload's `needs_breakdown`.
    guarded: a failed read flags nothing."""
    try:
        return snapshot(user_uuid).flags
    except Exception:
        logger.exception("breakdown flags failed for %s; flagging nothing",
                         user_uuid)
        return {}


# --- the digest ----------------------------------------------------------------


def _q(title: str) -> str:
    return f'"{title}"'


def _titles_line(titles: list) -> str:
    shown = " / ".join(_q(t) for t in titles[:_LINE_CAP])
    more = len(titles) - _LINE_CAP
    return shown + (f" (+{more} more)" if more > 0 else "")


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def render(fd: FocusDay) -> Optional[str]:
    """the ambient block. a day with no events, no finishes and nothing
    planned renders nothing; lines with nothing to say are omitted."""
    if not (fd.happened or fd.planned or fd.drifts or fd.rewinds):
        return None
    when = "today" if fd.live else "yesterday"
    if fd.live:
        head = ("today so far (from their desk - background awareness, "
                "they haven't seen this):")
    else:
        head = (f"yesterday, {fd.day.strftime('%a %b')} {fd.day.day} "
                "(from their desk):")
    lines = [head]

    if fd.runs:
        per_task: dict = {}
        for r in fd.runs:
            key = r.task_id if r.task_id is not None else r.label
            secs, n = per_task.get(key, (0, 0))
            per_task[key] = (secs + r.seconds, n + 1)
        ranked = sorted(per_task.items(), key=lambda kv: -kv[1][0])
        parts = []
        for key, (secs, n) in ranked[:_LINE_CAP]:
            title = fd.title_of(key if isinstance(key, int) else None,
                                key if isinstance(key, str) else "")
            runs = f" ({n} runs)" if n > 1 else ""
            parts.append(f"{_q(title)} {secs // 60} min{runs}")
        total = fd.banked_seconds // 60
        lines.append(f"banked: {total} min in {fd.run_count} "
                     f"run{'s' if fd.run_count != 1 else ''} - "
                     + " / ".join(parts))
    if fd.finished:
        lines.append(f"finished {when}: {_titles_line(fd.finished)}")
    if fd.live:
        if fd.running:
            r = fd.running
            target = (f", target {r.target_minutes}"
                      if r.target_minutes else "")
            lines.append(f"right now: clock running on {_q(r.label)} - "
                         f"{r.minutes_in} min in{target} "
                         f"(since {_clock(r.since)})")
        elif fd.frozen:
            r = fd.frozen
            lines.append(f"right now: clock stopped on {_q(r.label)} at "
                         f"{r.minutes_in} min, unbanked - a rewind question "
                         "is waiting for their answer")
        elif fd.runs:
            last = fd.runs[-1]
            lines.append(f"last run ended {fd.idle_minutes} min ago "
                         f"({last.reason} {_q(last.label)} at "
                         f"{last.seconds // 60} min)")
        else:
            lines.append("no runs yet today")
    if fd.untouched:
        lines.append(f"untouched {when}: {_titles_line(fd.untouched)}")
    if fd.set_aside:
        lines.append(f"set aside {when}: {_titles_line(fd.set_aside)}")
    if fd.drifts:
        if fd.returns >= fd.drifts:
            back = ("came back" if fd.drifts == 1 else
                    "came back both times" if fd.drifts == 2 else
                    "came back each time")
        elif fd.returns == 0:
            back = "hasn't come back" if fd.live else "didn't come back"
        else:
            back = f"came back {fd.returns} of {fd.drifts} times"
        lines.append(f"drifted {_times(fd.drifts)} mid-block, {back}")
    if fd.rewinds:
        lines.append(f"rewound the clock {_times(fd.rewinds)}")
    return "\n".join(lines)


def digest(user_uuid: str, day: Optional[date] = None) -> Optional[str]:
    """render(snapshot(...)), guarded: failure = no block."""
    try:
        return render(snapshot(user_uuid, day))
    except Exception:
        logger.exception("focus day digest failed for %s; continuing "
                         "without", user_uuid)
        return None


# --- the posture -----------------------------------------------------------------


def first_block(fd: FocusDay) -> Optional[dict]:
    """the likeliest tiny piece from what's planned: the oldest carried-over
    task, else the smallest estimate - preferring tasks that aren't flagged
    as stuck, so the invitation isn't the piece that keeps not happening."""
    if not fd.planned:
        return None
    today = fd.day.isoformat()
    calm = [t for t in fd.planned if t["id"] not in fd.flags] or fd.planned
    overdue = [t for t in calm if t["scheduled"] and t["scheduled"] < today]
    pick = overdue[0] if overdue else min(
        calm, key=lambda t: (t["pom_estimate"] is None,
                             t["pom_estimate"] or 0, t["id"]))
    return {"title": pick["title"], "next_action": pick["next_action"]}


def checkin_posture(fd: FocusDay) -> tuple:
    """(posture, detail) for a scheduled tick (§5.3, §10.2). one posture,
    chosen in this order: a running clock is always mid_block; a day
    whose work is done - or any evening that banked something - wraps;
    a stuck task with no clock names itself; a planned day nothing has
    started on invites one first block; banked runs and an idle clock
    are between; nothing at all is a quiet day."""
    if fd.running:
        r = fd.running
        return POSTURE_MID_BLOCK, {
            "label": r.label, "minutes_in": r.minutes_in,
            "target_minutes": r.target_minutes}
    evening = fd.now.hour >= Config.DAY_WRAP_HOUR
    worked = bool(fd.runs or fd.finished)
    if worked and (not fd.planned or evening):
        return POSTURE_WRAPPED, {"evening": evening,
                                 "banked_minutes": fd.banked_seconds // 60,
                                 "finished": list(fd.finished)}
    if fd.flags:
        # the task with the most evidence; ties fall to list order
        task_id = max(fd.flags, key=lambda k: (len(fd.flags[k]), -k))
        return POSTURE_STUCK, {"title": fd.title_of(task_id),
                               "signals": list(fd.flags[task_id])}
    if not fd.runs and fd.planned:
        return POSTURE_UNTOUCHED, {"first": first_block(fd)}
    if fd.runs:
        last = fd.runs[-1]
        return POSTURE_BETWEEN, {
            "idle_minutes": fd.idle_minutes,
            "banked_minutes": fd.banked_seconds // 60,
            "last_label": last.label,
            "planned_left": len(fd.planned)}
    return POSTURE_QUIET_DAY, {}

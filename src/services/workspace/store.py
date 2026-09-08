"""WorkspaceStore: the one write path for the native workspace.

every mutation - tools, reconciler, importer, seed scripts - goes through
here, so the invariants live in exactly one place:

- closed_at stamping (design section 2.0): status entering an entity's closed
  set stamps closed_at; reopening clears it. nothing hard-deletes.
- plans.last_activity_at: stamped as a side effect of any related write (task
  under the plan, goal change, win logged, note attached, check-in touching
  it), so dormancy is a column read, not an event-log scan.
- goal/plan consistency: a task pointing at a goal must share that goal's
  plan (the plan is inherited when unset).
- reschedule accounting: `scheduled` slipping to a LATER date bumps
  `reschedules`; pulling work earlier costs nothing.
- occasion recurrence: `date` always holds the next occurrence; the store
  rolls it forward past occurrences on read.
- cross-user isolation: public numeric ids are only unique per user, so every
  query here filters by user_uuid - no exceptions.

methods are sync (the managers pattern): plain queries over get_db()
sessions; async tool handlers just call them. rows come back as plain dicts
(JSON-safe, promptable - dates as ISO strings) with `public_id` attached.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from src.database.database import get_db
from src.database.models import (Plan, Goal, Task, Cycle, CycleBaseline,
                                 Win, Checkin, Note, Occasion)
from src.services.workspace import vocab
from src.utils.timezone_utils import utc_now

_MODELS = {
    "plan": Plan, "goal": Goal, "task": Task, "cycle": Cycle,
    "win": Win, "checkin": Checkin, "note": Note, "occasion": Occasion,
}


@dataclass
class ResolutionResult:
    """outcome of a name-or-id lookup. exactly one of these shapes:
    match set (unique hit), candidates set (ambiguous - the caller lists them
    instead of guessing), or both empty (nothing found)."""
    match: Optional[dict] = None
    candidates: list[dict] = field(default_factory=list)


def _coerce_date(value) -> Optional[date]:
    """accept date objects or ISO strings (tools pass strings)."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


# the scope line's cap (docs/FOCUS_DOGFOOD_DESIGN.md section 3) - one first
# piece, not a paragraph; `description` keeps the long-form role
NEXT_ACTION_CAP = 140


def _clean_scope(value: Optional[str]) -> Optional[str]:
    """strip and cap a next_action; blank collapses to None (unscoped)."""
    if value is None:
        return None
    value = str(value).strip()[:NEXT_ACTION_CAP].strip()
    return value or None


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _next_occurrence(d: date, recurrence: str, today: date) -> date:
    """first occurrence >= today. monthly/yearly step from the ANCHOR date
    with cumulative offsets, so a rent-on-the-31st occasion clamps to feb 28
    / jun 30 in short months but snaps back to the 31st - iterating clamped
    dates would drift the day down permanently."""
    if recurrence == "weekly":
        while d < today:
            d += timedelta(days=7)
        return d
    step = 12 if recurrence == "yearly" else 1
    k = 1
    while (nd := _add_months(d, k * step)) < today:
        k += 1
    return nd


class WorkspaceStore:

    # --- shared internals ----------------------------------------------------

    def _get_owned(self, db, model, user_uuid: str, row_id: int, label: str):
        """fetch a row by id, scoped to the owner - the isolation backbone.
        a foreign id raises the same error as a missing one on purpose:
        existence of another user's rows is never revealed."""
        row = db.query(model).filter(
            model.user_uuid == user_uuid, model.id == row_id).first()
        if row is None:
            raise ValueError(f"{label} {row_id} not found")
        return row

    def _touch_plan(self, db, user_uuid: str, plan_id: Optional[int]) -> None:
        """stamp last_activity_at on a plan as a side effect of related writes."""
        if plan_id is None:
            return
        plan = db.query(Plan).filter(
            Plan.user_uuid == user_uuid, Plan.id == plan_id).first()
        if plan is not None:
            plan.last_activity_at = utc_now()

    def _apply_status(self, row, entity: str, new_status: str) -> None:
        """set a canonical status and keep closed_at honest (section 2.0)."""
        row.status = new_status
        if vocab.is_closed_status(entity, new_status):
            if row.closed_at is None:
                row.closed_at = utc_now()
        else:
            row.closed_at = None

    def _plan_title_map(self, db, user_uuid: str, ids: set) -> dict:
        ids.discard(None)
        if not ids:
            return {}
        rows = db.query(Plan.id, Plan.title).filter(
            Plan.user_uuid == user_uuid, Plan.id.in_(ids)).all()
        return dict(rows)

    # --- dict builders -------------------------------------------------------

    def _plan_dict(self, row: Plan) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("plan", row.id),
            "title": row.title, "helper": row.helper, "status": row.status,
            "why": row.why, "success_criteria": row.success_criteria,
            "horizon_start": _iso(row.horizon_start), "horizon_end": _iso(row.horizon_end),
            "cadence": row.cadence, "legacy_area": row.legacy_area,
            "last_activity_at": _iso(row.last_activity_at),
            "created_at": _iso(row.created_at), "closed_at": _iso(row.closed_at),
        }

    def _goal_dict(self, row: Goal, plan_title: Optional[str] = None) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("goal", row.id),
            "title": row.title, "status": row.status,
            "plan_id": row.plan_id, "plan_title": plan_title,
            "target": _iso(row.target), "done_means": row.done_means,
            "created_at": _iso(row.created_at), "closed_at": _iso(row.closed_at),
        }

    def _task_dict(self, row: Task, plan_title=None, goal_title=None, cycle_title=None) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("task", row.id),
            "title": row.title, "status": row.status, "priority": row.priority,
            "scheduled": _iso(row.scheduled), "window": row.window,
            "pom_estimate": row.pom_estimate,
            "plan_id": row.plan_id, "plan_title": plan_title,
            "goal_id": row.goal_id, "goal_title": goal_title,
            "cycle_id": row.cycle_id, "cycle_title": cycle_title,
            "helper": row.helper, "reschedules": row.reschedules or 0,
            "description": row.description,
            "next_action": row.next_action,
            "set_aside_on": _iso(row.set_aside_on),
            "set_aside_count": row.set_aside_count or 0,
            "set_aside_counted_on": _iso(row.set_aside_counted_on),
            "breakdown_offer_dismissed_at": _iso(row.breakdown_offer_dismissed_at),
            "created_at": _iso(row.created_at), "closed_at": _iso(row.closed_at),
        }

    def _cycle_dict(self, row: Cycle) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("cycle", row.id),
            "title": row.title, "status": row.status,
            "start_date": _iso(row.start_date), "end_date": _iso(row.end_date),
            "goal": row.goal, "focus": row.focus,
            "theme": row.theme, "capacity_blocks": row.capacity_blocks,
            "created_at": _iso(row.created_at), "closed_at": _iso(row.closed_at),
        }

    def _win_dict(self, row: Win, plan_title: Optional[str] = None) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("win", row.id),
            "title": row.title, "date": _iso(row.date), "helper": row.helper,
            "plan_id": row.plan_id, "plan_title": plan_title,
            "task_id": row.task_id, "evidence": row.evidence,
            "weight": row.weight, "created_at": _iso(row.created_at),
        }

    def _checkin_dict(self, row: Checkin) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("checkin", row.id),
            "date": _iso(row.date), "kind": row.kind, "energy": row.energy,
            "notes": row.notes, "plan_ids": list(row.plan_ids or []),
            "helper": row.helper, "created_at": _iso(row.created_at),
        }

    def _note_dict(self, row: Note, plan_title: Optional[str] = None) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("note", row.id),
            "title": row.title, "body": row.body, "status": row.status,
            "plan_id": row.plan_id, "plan_title": plan_title,
            "tags": list(row.tags or []), "helper": row.helper,
            "promoted_plan_id": row.promoted_plan_id,
            "promoted_task_id": row.promoted_task_id,
            "created_at": _iso(row.created_at), "closed_at": _iso(row.closed_at),
        }

    def _occasion_dict(self, row: Occasion, plan_title: Optional[str] = None) -> dict:
        return {
            "id": row.id, "public_id": vocab.public_id("occasion", row.id),
            "title": row.title, "date": _iso(row.date), "time": row.time,
            "recurrence": row.recurrence,
            "plan_id": row.plan_id, "plan_title": plan_title,
            "notes": row.notes, "helper": row.helper,
            "created_at": _iso(row.created_at),
        }

    # --- plans ---------------------------------------------------------------

    def create_plan(self, user_uuid: str, title: str, helper: str, *,
                    status: str = "proposed", why: Optional[str] = None,
                    success_criteria: Optional[str] = None,
                    horizon_start=None, horizon_end=None,
                    cadence: Optional[str] = None,
                    legacy_area: Optional[str] = None,
                    notion_page_id: Optional[str] = None) -> dict:
        status = vocab.canonical_status("plan", status)
        if cadence is not None:
            cadence = vocab.canonical_value(cadence, vocab.PLAN_CADENCE, "cadence")
        with get_db() as db:
            row = Plan(
                user_uuid=user_uuid, title=title, helper=helper,
                why=why, success_criteria=success_criteria,
                horizon_start=_coerce_date(horizon_start),
                horizon_end=_coerce_date(horizon_end),
                cadence=cadence, legacy_area=legacy_area,
                notion_page_id=notion_page_id,
                last_activity_at=utc_now(),
            )
            self._apply_status(row, "plan", status)
            db.add(row)
            db.flush()
            return self._plan_dict(row)

    def update_plan(self, user_uuid: str, plan_id: int, **changes) -> dict:
        allowed = {"title", "helper", "status", "why", "success_criteria",
                   "horizon_start", "horizon_end", "cadence"}
        self._check_keys(changes, allowed, "plan")
        with get_db() as db:
            row = self._get_owned(db, Plan, user_uuid, plan_id, "plan")
            if "status" in changes:
                self._apply_status(row, "plan", vocab.canonical_status("plan", changes.pop("status")))
            if "cadence" in changes:
                cadence = changes.pop("cadence")
                row.cadence = None if cadence is None else vocab.canonical_value(
                    cadence, vocab.PLAN_CADENCE, "cadence")
            for key in ("horizon_start", "horizon_end"):
                if key in changes:
                    setattr(row, key, _coerce_date(changes.pop(key)))
            for key, value in changes.items():
                setattr(row, key, value)
            row.last_activity_at = utc_now()
            db.flush()
            return self._plan_dict(row)

    def get_plan(self, user_uuid: str, plan_id: int) -> dict:
        with get_db() as db:
            return self._plan_dict(self._get_owned(db, Plan, user_uuid, plan_id, "plan"))

    def list_plans(self, user_uuid: str, *, helper: Optional[str] = None,
                   status: Optional[str] = None, include_closed: bool = False) -> list[dict]:
        with get_db() as db:
            q = db.query(Plan).filter(Plan.user_uuid == user_uuid)
            if status is not None:
                q = q.filter(Plan.status == vocab.canonical_status("plan", status))
            elif not include_closed:
                q = q.filter(Plan.status.in_(vocab.PLAN_STATUS_OPEN))
            if helper is not None:
                q = q.filter(Plan.helper == helper)
            return [self._plan_dict(r) for r in q.order_by(Plan.id).all()]

    # --- goals ---------------------------------------------------------------

    def create_goal(self, user_uuid: str, plan_id: int, title: str, *,
                    status: str = "not_started", target=None,
                    done_means: Optional[str] = None) -> dict:
        status = vocab.canonical_status("goal", status)
        with get_db() as db:
            plan = self._get_owned(db, Plan, user_uuid, plan_id, "plan")
            row = Goal(user_uuid=user_uuid, plan_id=plan.id, title=title,
                       target=_coerce_date(target), done_means=done_means)
            self._apply_status(row, "goal", status)
            db.add(row)
            plan.last_activity_at = utc_now()
            db.flush()
            return self._goal_dict(row, plan_title=plan.title)

    def update_goal(self, user_uuid: str, goal_id: int, **changes) -> dict:
        allowed = {"title", "status", "target", "done_means"}
        self._check_keys(changes, allowed, "goal")
        with get_db() as db:
            row = self._get_owned(db, Goal, user_uuid, goal_id, "goal")
            if "status" in changes:
                self._apply_status(row, "goal", vocab.canonical_status("goal", changes.pop("status")))
            if "target" in changes:
                row.target = _coerce_date(changes.pop("target"))
            for key, value in changes.items():
                setattr(row, key, value)
            self._touch_plan(db, user_uuid, row.plan_id)
            db.flush()
            titles = self._plan_title_map(db, user_uuid, {row.plan_id})
            return self._goal_dict(row, plan_title=titles.get(row.plan_id))

    def list_goals(self, user_uuid: str, *, plan_id: Optional[int] = None,
                   include_closed: bool = False) -> list[dict]:
        with get_db() as db:
            q = db.query(Goal).filter(Goal.user_uuid == user_uuid)
            if plan_id is not None:
                q = q.filter(Goal.plan_id == plan_id)
            if not include_closed:
                q = q.filter(Goal.status.in_(vocab.GOAL_STATUS_OPEN))
            rows = q.order_by(Goal.id).all()
            titles = self._plan_title_map(db, user_uuid, {r.plan_id for r in rows})
            return [self._goal_dict(r, plan_title=titles.get(r.plan_id)) for r in rows]

    # --- tasks ---------------------------------------------------------------

    def _resolve_task_links(self, db, user_uuid: str, plan_id, goal_id, cycle_id):
        """validate ownership of every link and enforce goal/plan consistency
        (inheriting the goal's plan when the task doesn't name one)."""
        if cycle_id is not None:
            self._get_owned(db, Cycle, user_uuid, cycle_id, "cycle")
        goal = None
        if goal_id is not None:
            goal = self._get_owned(db, Goal, user_uuid, goal_id, "goal")
        if plan_id is not None:
            self._get_owned(db, Plan, user_uuid, plan_id, "plan")
        if goal is not None:
            if plan_id is None:
                plan_id = goal.plan_id
            elif plan_id != goal.plan_id:
                raise ValueError(
                    f"goal {vocab.public_id('goal', goal.id)} belongs to plan "
                    f"{vocab.public_id('plan', goal.plan_id)}, not "
                    f"{vocab.public_id('plan', plan_id)}")
        return plan_id, goal_id, cycle_id

    def create_task(self, user_uuid: str, title: str, *,
                    status: str = "todo", priority: Optional[str] = None,
                    scheduled=None, window: Optional[str] = None,
                    pom_estimate: Optional[float] = None,
                    plan_id: Optional[int] = None, goal_id: Optional[int] = None,
                    cycle_id: Optional[int] = None, helper: Optional[str] = None,
                    description: Optional[str] = None,
                    next_action: Optional[str] = None,
                    notion_page_id: Optional[str] = None) -> dict:
        status = vocab.canonical_status("task", status)
        if priority is not None:
            priority = vocab.canonical_value(priority, vocab.TASK_PRIORITY, "priority")
        if window is not None:
            window = vocab.canonical_value(window, vocab.TASK_WINDOW, "window")
        with get_db() as db:
            plan_id, goal_id, cycle_id = self._resolve_task_links(
                db, user_uuid, plan_id, goal_id, cycle_id)
            row = Task(
                user_uuid=user_uuid, title=title, priority=priority,
                scheduled=_coerce_date(scheduled), window=window,
                pom_estimate=pom_estimate, plan_id=plan_id, goal_id=goal_id,
                cycle_id=cycle_id, helper=helper, description=description,
                next_action=_clean_scope(next_action),
                notion_page_id=notion_page_id, reschedules=0,
                set_aside_count=0,
            )
            self._apply_status(row, "task", status)
            db.add(row)
            self._touch_plan(db, user_uuid, plan_id)
            db.flush()
            return self._task_dict(row, *self._task_titles(db, user_uuid, row))

    def update_task(self, user_uuid: str, task_id: int, **changes) -> dict:
        allowed = {"title", "status", "priority", "scheduled", "window",
                   "pom_estimate", "plan_id", "goal_id", "cycle_id",
                   "helper", "description", "next_action", "set_aside_on",
                   "breakdown_offer_dismissed_at"}
        self._check_keys(changes, allowed, "task")
        with get_db() as db:
            row = self._get_owned(db, Task, user_uuid, task_id, "task")
            if {"plan_id", "goal_id", "cycle_id"} & changes.keys():
                plan_id, goal_id, cycle_id = self._resolve_task_links(
                    db, user_uuid,
                    changes.pop("plan_id", row.plan_id),
                    changes.pop("goal_id", row.goal_id),
                    changes.pop("cycle_id", row.cycle_id))
                row.plan_id, row.goal_id, row.cycle_id = plan_id, goal_id, cycle_id
            if "status" in changes:
                self._apply_status(row, "task", vocab.canonical_status("task", changes.pop("status")))
            if "priority" in changes:
                p = changes.pop("priority")
                row.priority = None if p is None else vocab.canonical_value(
                    p, vocab.TASK_PRIORITY, "priority")
            if "window" in changes:
                w = changes.pop("window")
                row.window = None if w is None else vocab.canonical_value(
                    w, vocab.TASK_WINDOW, "window")
            if "scheduled" in changes:
                new = _coerce_date(changes.pop("scheduled"))
                # a slip to a LATER date is a reschedule; pulling work earlier
                # (or scheduling for the first time) costs nothing
                if new is not None and row.scheduled is not None and new > row.scheduled:
                    row.reschedules = (row.reschedules or 0) + 1
                row.scheduled = new
            if "next_action" in changes:
                row.next_action = _clean_scope(changes.pop("next_action"))
            if "set_aside_on" in changes:
                new = _coerce_date(changes.pop("set_aside_on"))
                # parking on a NEW day is the signal the breakdown offer
                # reads (section 10: "set aside on >= 2 distinct days").
                # the last counted day lives in its own column because
                # "bring back" clears set_aside_on - comparing against the
                # stamp would count a same-day repark twice (sol, #86).
                # re-stamping a counted day, or clearing, counts nothing
                if new is not None and new != row.set_aside_counted_on:
                    row.set_aside_count = (row.set_aside_count or 0) + 1
                    row.set_aside_counted_on = new
                row.set_aside_on = new
            for key, value in changes.items():
                setattr(row, key, value)
            self._touch_plan(db, user_uuid, row.plan_id)
            db.flush()
            return self._task_dict(row, *self._task_titles(db, user_uuid, row))

    def _task_titles(self, db, user_uuid: str, row: Task):
        plan_title = goal_title = cycle_title = None
        if row.plan_id:
            plan_title = self._plan_title_map(db, user_uuid, {row.plan_id}).get(row.plan_id)
        if row.goal_id:
            g = db.query(Goal.title).filter(
                Goal.user_uuid == user_uuid, Goal.id == row.goal_id).first()
            goal_title = g[0] if g else None
        if row.cycle_id:
            c = db.query(Cycle.title).filter(
                Cycle.user_uuid == user_uuid, Cycle.id == row.cycle_id).first()
            cycle_title = c[0] if c else None
        return plan_title, goal_title, cycle_title

    def list_tasks(self, user_uuid: str, *, status: Optional[str] = None,
                   plan_id: Optional[int] = None, goal_id: Optional[int] = None,
                   cycle_id: Optional[int] = None, scheduled_on=None,
                   scheduled_on_or_after=None, scheduled_on_or_before=None,
                   include_closed: bool = False, limit: Optional[int] = None) -> list[dict]:
        with get_db() as db:
            q = db.query(Task).filter(Task.user_uuid == user_uuid)
            if status is not None:
                q = q.filter(Task.status == vocab.canonical_status("task", status))
            elif not include_closed:
                q = q.filter(Task.status.in_(vocab.TASK_STATUS_OPEN))
            for col, val in ((Task.plan_id, plan_id), (Task.goal_id, goal_id),
                             (Task.cycle_id, cycle_id)):
                if val is not None:
                    q = q.filter(col == val)
            if scheduled_on is not None:
                q = q.filter(Task.scheduled == _coerce_date(scheduled_on))
            if scheduled_on_or_after is not None:
                q = q.filter(Task.scheduled >= _coerce_date(scheduled_on_or_after))
            if scheduled_on_or_before is not None:
                q = q.filter(Task.scheduled <= _coerce_date(scheduled_on_or_before))
            # scheduled-date order (nulls last), then id - the legacy list order
            q = q.order_by(Task.scheduled.is_(None), Task.scheduled, Task.id)
            if limit:
                q = q.limit(limit)
            rows = q.all()
            plan_titles = self._plan_title_map(db, user_uuid, {r.plan_id for r in rows})
            goal_ids = {r.goal_id for r in rows} - {None}
            goal_titles = dict(db.query(Goal.id, Goal.title).filter(
                Goal.user_uuid == user_uuid, Goal.id.in_(goal_ids)).all()) if goal_ids else {}
            cycle_ids = {r.cycle_id for r in rows} - {None}
            cycle_titles = dict(db.query(Cycle.id, Cycle.title).filter(
                Cycle.user_uuid == user_uuid, Cycle.id.in_(cycle_ids)).all()) if cycle_ids else {}
            return [self._task_dict(r, plan_titles.get(r.plan_id),
                                    goal_titles.get(r.goal_id),
                                    cycle_titles.get(r.cycle_id)) for r in rows]

    # --- cycles --------------------------------------------------------------

    def create_cycle(self, user_uuid: str, title: str, *,
                     status: str = "upcoming", start_date=None, end_date=None,
                     goal: Optional[str] = None, focus: Optional[str] = None,
                     theme: Optional[str] = None,
                     capacity_blocks: Optional[int] = None,
                     notion_page_id: Optional[str] = None) -> dict:
        status = vocab.canonical_status("cycle", status)
        with get_db() as db:
            row = Cycle(user_uuid=user_uuid, title=title,
                        start_date=_coerce_date(start_date),
                        end_date=_coerce_date(end_date),
                        goal=goal, focus=focus, theme=theme,
                        capacity_blocks=capacity_blocks,
                        notion_page_id=notion_page_id)
            self._apply_status(row, "cycle", status)
            db.add(row)
            db.flush()
            return self._cycle_dict(row)

    def update_cycle(self, user_uuid: str, cycle_id: int, **changes) -> dict:
        allowed = {"title", "status", "start_date", "end_date", "goal",
                   "focus", "theme", "capacity_blocks"}
        self._check_keys(changes, allowed, "cycle")
        with get_db() as db:
            row = self._get_owned(db, Cycle, user_uuid, cycle_id, "cycle")
            if "capacity_blocks" in changes and db.query(
                    CycleBaseline.id).filter(
                    CycleBaseline.cycle_id == row.id).first() is not None:
                # the section-6 invariant guards EVERY door, not just
                # CycleStore's: after the freeze, planned capacity moves
                # only through the scope-change ledger
                raise ValueError(
                    "this cycle's baseline is frozen - capacity moves only "
                    "through a scope change (change_scope with a reason)")
            if "status" in changes:
                self._apply_status(row, "cycle", vocab.canonical_status("cycle", changes.pop("status")))
            for key in ("start_date", "end_date"):
                if key in changes:
                    setattr(row, key, _coerce_date(changes.pop(key)))
            for key, value in changes.items():
                setattr(row, key, value)
            db.flush()
            return self._cycle_dict(row)

    def list_cycles(self, user_uuid: str, *, include_closed: bool = False) -> list[dict]:
        with get_db() as db:
            q = db.query(Cycle).filter(Cycle.user_uuid == user_uuid)
            if not include_closed:
                q = q.filter(Cycle.status.in_(vocab.CYCLE_STATUS_OPEN))
            return [self._cycle_dict(r) for r in q.order_by(Cycle.id).all()]

    def active_cycle(self, user_uuid: str) -> Optional[dict]:
        with get_db() as db:
            row = db.query(Cycle).filter(
                Cycle.user_uuid == user_uuid, Cycle.status == "active",
            ).order_by(Cycle.id.desc()).first()
            return self._cycle_dict(row) if row else None

    # --- wins ----------------------------------------------------------------

    def log_win(self, user_uuid: str, title: str, win_date, helper: str, *,
                plan_id: Optional[int] = None, task_id: Optional[int] = None,
                evidence: Optional[str] = None, weight: str = "solid") -> dict:
        weight = vocab.canonical_value(weight, vocab.WIN_WEIGHT, "weight")
        with get_db() as db:
            if task_id is not None:
                task = self._get_owned(db, Task, user_uuid, task_id, "task")
                if plan_id is None:
                    plan_id = task.plan_id   # a win born from a completion keeps the link
            if plan_id is not None:
                self._get_owned(db, Plan, user_uuid, plan_id, "plan")
            row = Win(user_uuid=user_uuid, title=title,
                      date=_coerce_date(win_date), helper=helper,
                      plan_id=plan_id, task_id=task_id,
                      evidence=evidence, weight=weight)
            db.add(row)
            self._touch_plan(db, user_uuid, plan_id)
            db.flush()
            titles = self._plan_title_map(db, user_uuid, {plan_id})
            return self._win_dict(row, plan_title=titles.get(plan_id))

    def list_wins(self, user_uuid: str, *, since=None, plan_id: Optional[int] = None,
                  weight: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
        with get_db() as db:
            q = db.query(Win).filter(Win.user_uuid == user_uuid)
            if since is not None:
                q = q.filter(Win.date >= _coerce_date(since))
            if plan_id is not None:
                q = q.filter(Win.plan_id == plan_id)
            if weight is not None:
                q = q.filter(Win.weight == vocab.canonical_value(
                    weight, vocab.WIN_WEIGHT, "weight"))
            q = q.order_by(Win.date.desc(), Win.id.desc())
            if limit:
                q = q.limit(limit)
            rows = q.all()
            titles = self._plan_title_map(db, user_uuid, {r.plan_id for r in rows})
            return [self._win_dict(r, plan_title=titles.get(r.plan_id)) for r in rows]

    # --- check-ins -----------------------------------------------------------

    def log_checkin(self, user_uuid: str, checkin_date, kind: str, helper: str, *,
                    energy: Optional[str] = None, notes: Optional[str] = None,
                    plan_ids: Optional[list] = None) -> dict:
        kind = vocab.canonical_value(kind, vocab.CHECKIN_KIND, "kind")
        if energy is not None:
            energy = vocab.canonical_value(energy, vocab.CHECKIN_ENERGY, "energy")
        with get_db() as db:
            resolved: list[int] = []
            for ref in plan_ids or []:
                pid = self._to_plan_id(ref)
                self._get_owned(db, Plan, user_uuid, pid, "plan")
                resolved.append(pid)
            row = Checkin(user_uuid=user_uuid, date=_coerce_date(checkin_date),
                          kind=kind, energy=energy, notes=notes,
                          plan_ids=resolved, helper=helper)
            db.add(row)
            for pid in resolved:
                self._touch_plan(db, user_uuid, pid)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                raise ValueError(
                    f"a {kind} check-in for {row.date} already exists "
                    "(adhoc check-ins are unlimited)") from None
            return self._checkin_dict(row)

    @staticmethod
    def _to_plan_id(ref) -> int:
        if isinstance(ref, int):
            return ref
        parsed = vocab.parse_public_id(str(ref))
        if parsed and parsed[0] == "plan":
            return parsed[1]
        raise ValueError(f"plan reference {ref!r} is not a plan id")

    def list_checkins(self, user_uuid: str, *, since=None, kind: Optional[str] = None,
                      limit: Optional[int] = None) -> list[dict]:
        with get_db() as db:
            q = db.query(Checkin).filter(Checkin.user_uuid == user_uuid)
            if since is not None:
                q = q.filter(Checkin.date >= _coerce_date(since))
            if kind is not None:
                q = q.filter(Checkin.kind == vocab.canonical_value(
                    kind, vocab.CHECKIN_KIND, "kind"))
            q = q.order_by(Checkin.date.desc(), Checkin.id.desc())
            if limit:
                q = q.limit(limit)
            return [self._checkin_dict(r) for r in q.all()]

    # --- notes ---------------------------------------------------------------

    def jot(self, user_uuid: str, body: str, *, title: Optional[str] = None,
            plan_id: Optional[int] = None, tags: Optional[list[str]] = None,
            helper: Optional[str] = None,
            notion_page_id: Optional[str] = None) -> dict:
        if not body or not body.strip():
            raise ValueError("a note needs a body")
        with get_db() as db:
            if plan_id is not None:
                self._get_owned(db, Plan, user_uuid, plan_id, "plan")
            row = Note(user_uuid=user_uuid, body=body, title=title,
                       plan_id=plan_id, tags=list(tags or []), helper=helper,
                       status="active", notion_page_id=notion_page_id)
            db.add(row)
            self._touch_plan(db, user_uuid, plan_id)
            db.flush()
            titles = self._plan_title_map(db, user_uuid, {plan_id})
            return self._note_dict(row, plan_title=titles.get(plan_id))

    def update_note(self, user_uuid: str, note_id: int, **changes) -> dict:
        allowed = {"body", "title", "plan_id", "tags", "status",
                   "promoted_plan_id", "promoted_task_id"}
        self._check_keys(changes, allowed, "note")
        with get_db() as db:
            row = self._get_owned(db, Note, user_uuid, note_id, "note")
            if "plan_id" in changes:
                pid = changes.pop("plan_id")
                if pid is not None:
                    self._get_owned(db, Plan, user_uuid, pid, "plan")
                row.plan_id = pid
            if "promoted_plan_id" in changes:
                pid = changes.pop("promoted_plan_id")
                if pid is not None:
                    self._get_owned(db, Plan, user_uuid, pid, "plan")
                row.promoted_plan_id = pid
            if "promoted_task_id" in changes:
                tid = changes.pop("promoted_task_id")
                if tid is not None:
                    self._get_owned(db, Task, user_uuid, tid, "task")
                row.promoted_task_id = tid
            if "status" in changes:
                self._apply_status(row, "note", vocab.canonical_status("note", changes.pop("status")))
            if "tags" in changes:
                row.tags = list(changes.pop("tags") or [])
            for key, value in changes.items():
                setattr(row, key, value)
            self._touch_plan(db, user_uuid, row.plan_id)
            db.flush()
            titles = self._plan_title_map(db, user_uuid, {row.plan_id})
            return self._note_dict(row, plan_title=titles.get(row.plan_id))

    def list_notes(self, user_uuid: str, *, plan_id: Optional[int] = None,
                   tag: Optional[str] = None, since=None,
                   query: Optional[str] = None,
                   include_closed: bool = False) -> list[dict]:
        with get_db() as db:
            q = db.query(Note).filter(Note.user_uuid == user_uuid)
            if plan_id is not None:
                q = q.filter(Note.plan_id == plan_id)
            if not include_closed:
                q = q.filter(Note.status.in_(vocab.NOTE_STATUS_OPEN))
            if since is not None:
                q = q.filter(Note.created_at >= _coerce_date(since))
            rows = q.order_by(Note.id.desc()).all()
            if tag is not None:
                # JSON containment queries aren't portable; note volumes are
                # small, so tag filtering happens in python on purpose
                rows = [r for r in rows if tag in (r.tags or [])]
            if query:
                needle = query.lower()
                rows = [r for r in rows
                        if needle in (r.title or "").lower() or needle in r.body.lower()]
            titles = self._plan_title_map(db, user_uuid, {r.plan_id for r in rows})
            return [self._note_dict(r, plan_title=titles.get(r.plan_id)) for r in rows]

    # --- occasions -----------------------------------------------------------

    def create_occasion(self, user_uuid: str, title: str, occasion_date, *,
                        time: Optional[str] = None, recurrence: Optional[str] = None,
                        plan_id: Optional[int] = None, notes: Optional[str] = None,
                        helper: Optional[str] = None) -> dict:
        if recurrence is not None:
            recurrence = vocab.canonical_value(
                recurrence, vocab.OCCASION_RECURRENCE, "recurrence")
        with get_db() as db:
            if plan_id is not None:
                self._get_owned(db, Plan, user_uuid, plan_id, "plan")
            row = Occasion(user_uuid=user_uuid, title=title,
                           date=_coerce_date(occasion_date), time=time,
                           recurrence=recurrence, plan_id=plan_id,
                           notes=notes, helper=helper)
            db.add(row)
            db.flush()
            titles = self._plan_title_map(db, user_uuid, {plan_id})
            return self._occasion_dict(row, plan_title=titles.get(plan_id))

    def update_occasion(self, user_uuid: str, occasion_id: int, **changes) -> dict:
        allowed = {"title", "date", "time", "recurrence", "plan_id", "notes"}
        self._check_keys(changes, allowed, "occasion")
        with get_db() as db:
            row = self._get_owned(db, Occasion, user_uuid, occasion_id, "occasion")
            if "recurrence" in changes:
                rec = changes.pop("recurrence")
                row.recurrence = None if rec is None else vocab.canonical_value(
                    rec, vocab.OCCASION_RECURRENCE, "recurrence")
            if "plan_id" in changes:
                pid = changes.pop("plan_id")
                if pid is not None:
                    self._get_owned(db, Plan, user_uuid, pid, "plan")
                row.plan_id = pid
            if "date" in changes:
                row.date = _coerce_date(changes.pop("date"))
            for key, value in changes.items():
                setattr(row, key, value)
            db.flush()
            titles = self._plan_title_map(db, user_uuid, {row.plan_id})
            return self._occasion_dict(row, plan_title=titles.get(row.plan_id))

    def list_occasions(self, user_uuid: str, *, today=None, until=None,
                       plan_id: Optional[int] = None,
                       include_past: bool = False) -> list[dict]:
        """upcoming occasions in date order. when `today` (the user-local
        date) is given, recurring occasions whose date has passed are first
        rolled forward and persisted - `date` always holds the next
        occurrence. one-off occasions in the past simply sort into history
        (visible via include_past)."""
        today = _coerce_date(today)
        with get_db() as db:
            if today is not None:
                stale = db.query(Occasion).filter(
                    Occasion.user_uuid == user_uuid,
                    Occasion.recurrence.isnot(None),
                    Occasion.date < today).all()
                for row in stale:
                    row.date = _next_occurrence(row.date, row.recurrence, today)
                db.flush()
            q = db.query(Occasion).filter(Occasion.user_uuid == user_uuid)
            if plan_id is not None:
                q = q.filter(Occasion.plan_id == plan_id)
            if today is not None and not include_past:
                q = q.filter(Occasion.date >= today)
            if until is not None:
                q = q.filter(Occasion.date <= _coerce_date(until))
            rows = q.order_by(Occasion.date, Occasion.id).all()
            titles = self._plan_title_map(db, user_uuid, {r.plan_id for r in rows})
            return [self._occasion_dict(r, plan_title=titles.get(r.plan_id)) for r in rows]

    # --- name-or-id resolution ------------------------------------------------

    _RESOLVE_DICTS = {
        "plan": "_plan_dict", "goal": "_goal_dict", "task": "_task_dict",
        "cycle": "_cycle_dict", "note": "_note_dict", "occasion": "_occasion_dict",
    }

    def resolve(self, user_uuid: str, entity: str, ref: str) -> ResolutionResult:
        """name-or-id lookup with the resolution ladder: public id short-circuits;
        otherwise exact title match, then case-insensitive exact, then substring -
        the first tier with ANY matches decides. a unique match resolves; several
        return candidates (the caller lists them instead of guessing); zero fall
        all the way through to an empty result."""
        model = _MODELS[entity]
        to_dict = getattr(self, self._RESOLVE_DICTS[entity])
        title_col = Note.title if entity == "note" else model.title
        with get_db() as db:
            parsed = vocab.parse_public_id(ref)
            if parsed is not None and parsed[0] == entity:
                row = db.query(model).filter(
                    model.user_uuid == user_uuid, model.id == parsed[1]).first()
                return ResolutionResult(match=to_dict(row)) if row else ResolutionResult()
            base = db.query(model).filter(model.user_uuid == user_uuid)
            tiers = [
                base.filter(title_col == ref),
                base.filter(title_col.ilike(_escape_like(ref), escape="\\")),
                base.filter(title_col.ilike(f"%{_escape_like(ref)}%", escape="\\")),
            ]
            for tier in tiers:
                rows = tier.order_by(model.id).all()
                if len(rows) == 1:
                    return ResolutionResult(match=to_dict(rows[0]))
                if rows:
                    return ResolutionResult(candidates=[to_dict(r) for r in rows])
            return ResolutionResult()

    # --- misc ----------------------------------------------------------------

    @staticmethod
    def _check_keys(changes: dict, allowed: set, label: str) -> None:
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(
                f"unknown {label} fields: {', '.join(sorted(unknown))}")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
